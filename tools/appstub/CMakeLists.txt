# tools/appstub/CMakeLists.txt -- the Windows tray companion.
#
# appstub is a Windows-only program (a notification-area icon is a Windows
# thing). On Linux the same job is done by systemd *user* units - see the
# README.
#
# It can be built on its own
#   cmake -S tools/appstub -B build-appstub && cmake --build build-appstub
# or as part of the tree (the default on Windows):
#   cmake -S . -B build && cmake --build build
# with -DTEA_BUILD_APPSTUB=OFF to leave it out.

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  cmake_minimum_required(VERSION 3.16)
  project(appstub CXX)
  set(TEA_APPSTUB_STANDALONE ON)
endif()

if(NOT WIN32)
  message(FATAL_ERROR
    "appstub is Windows-only. On Linux use systemd --user units instead "
    "(a.service for the agent, a2_task.service for the queue - see the "
    "tea_a2 repository).")
endif()

# ---------------------------------------------------------------------------
# appstub -- a tray host for other programs (Windows).
#
# One executable, no runtime dependencies beyond the system DLLs: the tray
# icon is a real icon resource, the configuration is read with the
# nlohmann/json header.
# ---------------------------------------------------------------------------

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

if(MSVC)
  # /utf-8: the sources are UTF-8 and the menu labels are Chinese.
  add_compile_options(/utf-8 /W3 /EHsc /permissive- /D_CRT_SECURE_NO_WARNINGS)
  add_link_options(/Brepro)
elseif(MINGW)
  add_compile_options(-Wall -Wextra -Wno-unused-parameter)
  # Reproducible builds: no timestamp in the PE header.
  add_link_options(-Wl,--no-insert-timestamp)
endif()

# The icon is a real resource (resources/appstub.ico): the shell draws
# the notification-area icon itself, and a hand-drawn HICON is not the same
# thing as an icon resource.
enable_language(RC)

add_executable(appstub WIN32 src/main.cpp resources/appstub.rc)

# nlohmann/json: from the tree when we are inside it (3rd/nlohmann_json), from
# the vendored copy when this directory is built on its own.
if(TARGET tea_3rd_headers)
  target_link_libraries(appstub PRIVATE tea_3rd_headers)
else()
  target_include_directories(appstub PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party/nlohmann_json)
endif()

# user32: the message loop, the tray icon and the menus.
# shell32: ShellExecute (opening a URL), Shell_NotifyIcon.
# ws2_32: the "is that port already in use" check before starting a child.
target_link_libraries(appstub PRIVATE user32 shell32 ws2_32)

# Next to tea_agent.exe, so one build directory holds everything.
set_target_properties(appstub PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin
)

# --- rebuild while the previous build is still running -----------------------
# Windows will not let the linker overwrite a running .exe (LNK1104), so the
# tray host can not be replaced while it is running - and the tray host is
# always running. PRE_LINK moves the old binary aside, POST_BUILD deletes the
# copy once the link has succeeded (cmake/free-running-exe.cmake).
if(WIN32 AND EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/../../cmake/free-running-exe.cmake)
  add_custom_command(TARGET appstub PRE_LINK
    COMMAND ${CMAKE_COMMAND}
            -DEXE=$<TARGET_FILE:appstub>
            -DMODE=prepare
            -P ${CMAKE_CURRENT_SOURCE_DIR}/../../cmake/free-running-exe.cmake
    COMMENT "Freeing appstub.exe for relinking"
    VERBATIM)
  add_custom_command(TARGET appstub POST_BUILD
    COMMAND ${CMAKE_COMMAND}
            -DEXE=$<TARGET_FILE:appstub>
            -DMODE=clean
            -P ${CMAKE_CURRENT_SOURCE_DIR}/../../cmake/free-running-exe.cmake
    VERBATIM)
endif()

install(TARGETS appstub RUNTIME DESTINATION bin)
