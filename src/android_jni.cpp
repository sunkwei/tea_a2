// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// android_jni.cpp -- the Android entry point.
//
// The APK loads liba2.so and starts the agent from Java:
//
//   A2Native.start(arrayOf("a2", "--port", "8383", "--tea-home", filesDir))
//
// The call returns at once: the agent runs on its own thread, because the
// Java caller is the UI thread and the agent only returns when it stops.
#ifdef __ANDROID__

#include <jni.h>

#include <stdlib.h>

#include <string>
#include <thread>
#include <vector>

#include "agent_main.h"

namespace {

std::vector<std::string> to_strings(JNIEnv* env, jobjectArray array) {
  std::vector<std::string> out;
  if (!array) return out;
  const jsize n = env->GetArrayLength(array);
  out.reserve(static_cast<std::size_t>(n));
  for (jsize i = 0; i < n; ++i) {
    jstring s = static_cast<jstring>(env->GetObjectArrayElement(array, i));
    if (!s) {
      out.push_back(std::string());
      continue;
    }
    const char* chars = env->GetStringUTFChars(s, nullptr);
    out.push_back(chars ? std::string(chars) : std::string());
    if (chars) env->ReleaseStringUTFChars(s, chars);
    env->DeleteLocalRef(s);
  }
  return out;
}

}  // namespace

extern "C" JNIEXPORT jint JNICALL
Java_com_tea_a2_A2Native_start(JNIEnv* env, jobject, jobjectArray argv) {
  // The strings are copied into the thread's closure: the array the caller
  // passed in may be collected as soon as this function returns.
  const std::vector<std::string> args = to_strings(env, argv);
  std::thread([args]() {
    std::vector<char*> raw;
    raw.reserve(args.size() + 1);
    for (const std::string& a : args) raw.push_back(const_cast<char*>(a.c_str()));
    raw.push_back(nullptr);
    tea::tea_agent_main(static_cast<int>(args.size()), raw.data());
  }).detach();
  return 0;
}

extern "C" JNIEXPORT void JNICALL
Java_com_tea_a2_A2Native_stop(JNIEnv*, jobject) {
  tea::agent_request_stop();
}

extern "C" JNIEXPORT void JNICALL
Java_com_tea_a2_A2Native_setEnv(JNIEnv* env, jobject, jstring name, jstring value) {
  // Android has no HOME: the agent resolves "~" and its own home directory
  // from the environment, so the app has to hand it one.
  if (!name || !value) return;
  const char* n = env->GetStringUTFChars(name, nullptr);
  const char* v = env->GetStringUTFChars(value, nullptr);
  if (n && v) setenv(n, v, 1);
  if (n) env->ReleaseStringUTFChars(name, n);
  if (v) env->ReleaseStringUTFChars(value, v);
}

#endif  // __ANDROID__
