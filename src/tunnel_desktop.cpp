// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop.cpp -- this machine's screen, and the mouse and keyboard on it.
//
// The work is done by the platform's own file:
//   * tunnel_desktop_win.cpp   (Windows): GDI and SendInput.
//   * tunnel_desktop_linux.cpp (Linux): the compositor's screenshot program
//     and a virtual device in the kernel.
//
// The two are picked by the build (see tunnel/CMakeLists.txt); this file
// only has to be in the list of sources.
#include "tunnel_desktop.h"
