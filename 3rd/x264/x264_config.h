// x264_config.h -- the configuration x264's own `configure` would write.
//
// We do not run x264's configure at build time (it is a shell script and the
// build has to work on a plain Windows box with just CMake and a compiler),
// so the few values it computes are written down here.
//
// SPDX-License-Identifier: GPL-2.0-or-later   (this is x264's own licence)
#ifndef X264_CONFIG_H
#define X264_CONFIG_H

#define X264_GPL           1
#define X264_INTERLACED    1
#define X264_BIT_DEPTH     8
#define X264_CHROMA_FORMAT 0
#define X264_VERSION "r165-stable"
#define X264_POINTVER "0.165.x"

#endif
