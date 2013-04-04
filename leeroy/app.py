# Copyright 2012 litl, LLC.  Licensed under the MIT license.

import github
import jenkins

import logging
import logging.config
import os
import sys

from flask import Flask, current_app, json, request, Response, abort
from optparse import OptionParser
from werkzeug.exceptions import NotFound

from github import register_github_hooks


app = Flask("leeroy")
app.config.from_object("leeroy.settings")

if "LEEROY_CONFIG" in os.environ:
    app.config.from_envvar("LEEROY_CONFIG")

logging_conf = app.config.get("LOGGING_CONF")
if logging_conf and os.path.exists(logging_conf):
    logging.config.fileConfig(logging_conf)

logger_name = app.config.get("LOGGER_NAME")
if logger_name:
    logging.root.name = logger_name


@app.route("/ping")
def ping():
    return "pong"


def _parse_jenkins_json(request):
    # The Jenkins notification plugin (at least as of 1.4) incorrectly sets
    # its Content-type as application/x-www-form-urlencoded instead of
    # application/json.  As a result, all of the data gets stored as a key
    # in request.form.  Try to detect that and deal with it.
    if len(request.form) == 1:
        try:
            return json.loads(request.form.keys()[0])
        except ValueError:
            # Seems bad that there's only 1 key, but press on
            return request.form
    else:
        return request.form


@app.route("/notification/jenkins", methods=["POST"])
def jenkins_notification():
    data = _parse_jenkins_json(request)

    jenkins_name = data["name"]
    jenkins_number = data["build"]["number"]
    jenkins_url = data["build"]["full_url"]
    phase = data["build"]["phase"]

    logging.debug("Received Jenkins notification for %s %s (%s): %s",
                  jenkins_name, jenkins_number, jenkins_url, phase)

    if phase not in ("STARTED", "COMPLETED"):
        return Response(status=204)

    git_app_repo = data["build"]["parameters"]["GIT_app_REPO"]
    git_sha1 = data["build"]["parameters"]["GIT_SHA1"]

    repo_config = github.get_repo_config(current_app, git_app_repo)

    if repo_config is None:
        err_msg = "No repo config for {0}".format(git_app_repo)
        logging.warn(err_msg)
        raise NotFound(err_msg)

    desc_prefix = "Jenkins build '{0}' #{1}".format(jenkins_name,
                                                    jenkins_number)

    if phase == "STARTED":
        github_state = "pending"
        github_desc = desc_prefix + " is running"
    else:
        status = data["build"]["status"]

        if status == "SUCCESS":
            github_state = "success"
            github_desc = desc_prefix + " has succeeded"
        elif status == "FAILURE" or status == "UNSTABLE":
            github_state = "failure"
            github_desc = desc_prefix + " has failed"
        elif status == "ABORTED":
            github_state = "error"
            github_desc = desc_prefix + " has encountered an error"
        else:
            logging.debug("Did not understand '%s' build status. Aborting.",
                          status)
            abort()

    logging.debug(github_desc)

    github.update_status(current_app,
                         repo_config,
                         git_app_repo,
                         git_sha1,
                         github_state,
                         github_desc,
                         jenkins_url)

    return Response(status=204)


@app.route("/notification/github", methods=["POST"])
def github_notification():
    action = request.json["action"]
    pull_request = request.json["pull_request"]
    number = pull_request["number"]
    html_url = pull_request["html_url"]
    app_repo_name = github.get_repo_name(pull_request, "app")

    logging.debug("Received GitHub pull request notification for "
                  "%s %s (%s): %s",
                  app_repo_name, number, html_url, action)

    if action not in ("opened", "synchronize"):
        logging.debug("Ignored '%s' action." % action)
        return Response(status=204)

    repo_config = github.get_repo_config(current_app, app_repo_name)

    if repo_config is None:
        err_msg = "No repo config for {0}".format(app_repo_name)
        logging.warn(err_msg)
        raise NotFound(err_msg)

    head_repo_name, shas = github.get_commits(current_app,
                                              repo_config,
                                              pull_request)

    logging.debug("Trigging builds for %d commits", len(shas))

    html_url = pull_request["html_url"]

    for sha in shas:
        github.update_status(current_app,
                             repo_config,
                             app_repo_name,
                             sha,
                             "pending",
                             "Jenkins build is being scheduled")

        logging.debug("Scheduling build for %s %s", head_repo_name, sha)
        jenkins.schedule_build(current_app,
                               repo_config,
                               head_repo_name,
                               sha,
                               html_url)

    return Response(status=204)


def run_app():
    parser = OptionParser()
    parser.add_option("-d", "--debug",
                      action="store_true", dest="debug", default=False,
                      help="activate the flask debugger")
    parser.add_option("-u", "--urls",
                      action="store_true", dest="urls", default=False,
                      help="list the url patterns used")
    parser.add_option("-b", "--bind-address",
                      action="store", type="string", dest="host",
                      default="0.0.0.0",
                      help="specify the address on which to listen")
    parser.add_option("-p", "--port",
                      action="store", type="int", dest="port",
                      default=5000,
                      help="specify the port number on which to run")

    (options, args) = parser.parse_args()

    if options.urls:
        from operator import attrgetter
        rules = sorted(app.url_map.iter_rules(), key=attrgetter("rule"))

        # don't show the less important HTTP methods
        skip_methods = set(["HEAD", "OPTIONS"])

        print "URL rules in use:"
        for rule in rules:
            methods = set(rule.methods).difference(skip_methods)

            print "  %s (%s)" % (rule.rule, " ".join(methods))

        sys.exit(0)

    app.debug = options.debug
    app.run(host=options.host, port=options.port)


register_github_hooks(app)


if __name__ == '__main__':
    run_app()
