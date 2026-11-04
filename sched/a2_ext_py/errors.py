"""One error type for everything this layer talks to.

The agent (a2) and the task queue answer with different shapes: the agent
says ``{"error":{"code":"…","message":"…"}}``, the queue says
``{"code":…, "message":…, "request_id":…, "data":{…}}``. Both mean the same
thing to a caller - "it did not do what you asked" - so both raise this.

Before this module there were three error types with three shapes
(``ApiError`` / ``AgentError`` / ``ChannelError``), which meant every caller
had to know which server it was talking to before it could catch anything.
"""


class ApiError(Exception):
    """The server answered, but not with what we asked for."""

    def __init__(self, status, message, body=""):
        super().__init__("%s: %s" % (status, message))
        self.status = status
        self.message = message
        self.body = body
