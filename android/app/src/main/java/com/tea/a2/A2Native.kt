package com.tea.a2

/**
 * The C++ agent, loaded from liba2.so.
 *
 * The library is the whole agent: the HTTP server, the tools, the web UI.
 * The app only starts it and hands it the command line it would have got on
 * a server.
 */
object A2Native {
    init {
        System.loadLibrary("a2")
    }

    /** Starts the agent with the given argv. Returns at once. */
    external fun start(argv: Array<String>): Int

    /** Asks the agent to stop. */
    external fun stop()

    /** Sets an environment variable for the agent (HOME, TMPDIR). */
    external fun setEnv(name: String, value: String)
}
