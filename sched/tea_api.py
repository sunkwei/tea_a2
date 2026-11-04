#!/usr/bin/env python3
"""The clients of tea_agent's primitive API and of the task queue.

**Moved.** These classes now live in `a2_ext_py` (the Python extension layer
of a2) - there was one copy here, one in `channel.py` and one in
`task_server.py`, and they had drifted. This module stays as the old name,
so scripts that import it keep working:

    from tea_api import Tea, Queue      # old
    from a2_ext_py import Client, Queue # new

The point of the primitive layer is that a *policy* - when to work, what to
work on, when to stop, who to tell - lives in a script, not in the server.
This module is the vocabulary those scripts speak: one model call, the task
records, the provider catalogue, the quota, and the event stream.

There are **two servers** behind that vocabulary, and they are not the same
thing:

  tea_agent (C++)      the workspace channel: the model call, the providers,
                        the quota, the workspaces.  -> class `Client`
  task_server (Python) the queue: the task records, the events. -> class `Queue`

The task routes used to live on the agent; they moved to the Python side with
the queue, so a script that wants the tasks talks to `Queue`, not to `Client`.

  from a2_ext_py import Client, Queue

  client = Client("http://127.0.0.1:8282")
  quota = client.usage("minimax")            # how much is left

  queue = Queue("http://127.0.0.1:8283")
  task = queue.create_task(ws, "do the thing")
  queue.start_task(ws, task["id"])
  for event in queue.events():
      if event["type"] == "task_done":
          notify(event["task"])

Standard library only: these scripts run on the device next to the agent.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from a2_ext_py import ApiError, Client, Queue  # noqa: E402

# The old names, so nothing breaks.
Tea = Client

__all__ = ["ApiError", "Client", "Queue", "Tea"]
