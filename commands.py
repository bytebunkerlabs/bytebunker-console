"""Every command ByteBunker has, in one table: the CLI's subcommands, the
chat's slash commands and the HTTP routes behind them. bb reads it for its
help and its dispatch, the chat (bb's REPL, and the Playground) for its
slash commands, and a test checks that each entry has its handler and its
route, so the faces cannot drift apart.

    cli      the bb subcommand ("ask", "sessions ls"), or None
    slash    the chat command ("/model"), or None
    args     how it is called, for help
    route    the HTTP route it uses ("GET /api/models"), or None
    help     one line
"""

COMMANDS = [
    # chat
    {"cli": "ask", "slash": None, "args": "TEXT [-m MODEL] [-e EFFORT] [-p PROFILE] [--json]",
     "route": "POST /api/sessions/<id>/turns", "help": "one question, answered here; piped input is attached"},
    {"cli": "chat", "slash": None, "args": "[SESSION]", "route": "POST /api/sessions/<id>/turns",
     "help": "chat in this terminal (also: bb with no arguments)"},
    {"cli": None, "slash": "/model", "args": "[MODEL]", "route": "GET /api/models",
     "help": "show the model, or switch to another"},
    {"cli": None, "slash": "/effort", "args": "[off|low|medium|high|max|default]", "route": None,
     "help": "how hard the model thinks, for the models that offer it"},
    {"cli": None, "slash": "/profile", "args": "[NAME]", "route": "GET /api/profiles",
     "help": "show the profile, or switch to another"},
    {"cli": None, "slash": "/tools", "args": "[on|off]", "route": "GET /api/tools",
     "help": "list the tools the model can use, or turn them on or off"},
    {"cli": None, "slash": "/skills", "args": "[NAME ...]", "route": "GET /api/skills",
     "help": "list skills, or attach these to the chat"},
    {"cli": None, "slash": "/attach", "args": "FILE ...", "route": "POST /api/upload",
     "help": "attach files to the next message"},
    {"cli": None, "slash": "/compress", "args": "", "route": "POST /api/sessions/<id>/compress",
     "help": "fold older turns into a summary"},
    {"cli": None, "slash": "/new", "args": "", "route": None, "help": "start a new session"},
    {"cli": None, "slash": "/open", "args": "SESSION", "route": "GET /api/sessions", "help": "continue a session"},
    {"cli": None, "slash": "/help", "args": "", "route": None, "help": "these commands"},
    {"cli": None, "slash": "/quit", "args": "", "route": None, "help": "leave (Ctrl-D does too)"},
    # sessions
    {"cli": "sessions ls", "slash": "/sessions", "args": "[-n N]", "route": "GET /api/sessions",
     "help": "recent sessions, from the app and from bb"},
    {"cli": "sessions show", "slash": None, "args": "SESSION", "route": "GET /api/sessions",
     "help": "a session's messages"},
    # models and servers
    {"cli": "models", "slash": "/models", "args": "", "route": "GET /api/models",
     "help": "models every gateway serves"},
    {"cli": "gateways", "slash": None, "args": "", "route": "GET /api/gateways",
     "help": "the model servers this app talks to"},
    {"cli": "monitors", "slash": None, "args": "", "route": "GET /api/monitors",
     "help": "rack monitors"},
    {"cli": "cluster", "slash": None, "args": "", "route": "GET /api/cluster",
     "help": "every node and engine the monitors see"},
    {"cli": "usage", "slash": "/usage", "args": "", "route": "GET /api/usage",
     "help": "tokens and throughput, 14 days"},
    {"cli": "skills", "slash": None, "args": "", "route": "GET /api/skills", "help": "skills"},
    {"cli": "mcp", "slash": None, "args": "", "route": "GET /api/tools", "help": "MCP servers and their tools"},
    # work that runs on its own
    {"cli": "agents", "slash": None, "args": "GOAL [--detach]", "route": "POST /api/agents",
     "help": "give the agents a goal and follow it"},
    {"cli": "agents ls", "slash": None, "args": "", "route": "GET /api/runs", "help": "agent runs"},
    {"cli": "agents stop", "slash": None, "args": "[RUN]", "route": "POST /api/runs/<id>/cancel",
     "help": "stop the goal that is running"},
    {"cli": "jobs ls", "slash": None, "args": "", "route": "GET /api/jobs", "help": "scheduled jobs"},
    {"cli": "jobs run", "slash": None, "args": "JOB", "route": "POST /api/jobs", "help": "run a job now and follow it"},
    {"cli": "runs ls", "slash": None, "args": "[-k KIND]", "route": "GET /api/runs",
     "help": "everything that ran or is running"},
    {"cli": "runs watch", "slash": None, "args": "RUN", "route": "GET /api/runs/<id>/events",
     "help": "follow a run from its start"},
    {"cli": "runs stop", "slash": None, "args": "RUN", "route": "POST /api/runs/<id>/cancel", "help": "stop a run"},
    # the server
    {"cli": "doctor", "slash": None, "args": "", "route": "GET /api/hello",
     "help": "is everything reachable, and what to do if not"},
    {"cli": "serve", "slash": None, "args": "", "route": None, "help": "run the server in this terminal"},
]


def cli_commands():
    return [c for c in COMMANDS if c["cli"]]


def slash_commands():
    return [c for c in COMMANDS if c["slash"]]


def routes():
    return sorted(set(c["route"] for c in COMMANDS if c["route"]))
