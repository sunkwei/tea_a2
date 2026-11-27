// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_idle.h -- the idle timeout of a tunnel, shared by both ends.
//
// A tunnel that carries no data for a long time is a tunnel nobody uses: it
// holds a process, a broker room, a TURN allocation and a relay port. The
// sidecar gives up on its own after --idle-timeout seconds of silence (0 =
// never) and leaves this exit code behind, so the agent on the other end can
// tell "the operator walked away" from "the sidecar crashed".
#pragma once

namespace tea_tunnel {

/// The exit code of a sidecar that stopped because the tunnel was idle.
const int kIdleExitCode = 3;

}  // namespace tea_tunnel
