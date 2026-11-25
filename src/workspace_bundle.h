// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// workspace_bundle.h -- 把一个工作区搬到另一台设备上去。
//
// 「工作区包」是一个 zip，里面装着这个工作区在这个设备上的全部对话资料：
//
//     manifest.json              清单（导出时间、工作区、每个主题的元信息）
//     sessions/<id>.json         每个主题（会话）的完整记录：消息、计划、摘要、产物
//     trajectories/<id>.jsonl    每个主题的轨迹（这一轮调了什么工具、输出是什么）
//     images/<topic>/<name>      贴进对话里的图片（消息里只存路径，字节在这里）
//     memory.json                记忆条目
//     agent.md                   这个工作区的经验（agent 自己写的）
//
// 导入的规矩只有一条：**本地的数据说了算**。本地已经有的主题不动它（导入报告
// 里说清哪些留了下来），没有的才写进去。所以同一个包导入两次，第二次什么也
// 不会变——搬家的动作可以重做，不会把人的话改掉。
#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "workspace.h"

namespace tea {

/// 一个包的边界。设备上的历史可能有几百兆，一次请求不该把设备榨干。
struct BundleLimits {
  /// 超过这个大小的文件不进包（要整个读进内存）。
  std::size_t max_file_bytes = 32u * 1024u * 1024u;
  /// 解压后的总量上限；超过之后剩下的文件不打包。
  unsigned long long max_total_bytes = 256ull * 1024u * 1024u;
  /// 包里最多多少个条目（防目录树病态）。
  std::size_t max_entries = 20000u;
};

/// 导出结果。`bytes` 就是那个 zip，可以直接发给浏览器。
struct BundleExport {
  bool ok = false;
  std::string bytes;
  int sessions = 0;       // 打包的主题数
  int trajectories = 0;   // 打包的轨迹数
  int images = 0;         // 打包的图片数
  int notes = 0;          // 打包的记忆条目数
  int skipped = 0;        // 太大或读不出来的文件（细节在日志里）
  std::string error;      // ok == false 时说明为什么
};

/// 导入结果：搬过来了什么、留下了什么。
struct BundleImport {
  int sessions = 0;                // 还原的主题数
  int sessions_conflict = 0;       // 本地已有、保留本地的主题数
  int trajectories = 0;            // 还原的轨迹数
  int trajectories_conflict = 0;   // 保留本地的轨迹数
  int images = 0;                 // 还原的图片数
  int notes = 0;                   // 并入的记忆条目数
  int notes_conflict = 0;          // 本地已有、保留本地的记忆条目数
  bool agent_doc = false;          // agent.md 被还原了
  bool agent_doc_conflict = false;  // 本地已有 agent.md，保留了本地那份
  std::vector<std::string> conflicts;  // 留了什么下来（人话）
  std::vector<std::string> warnings;   // 哪里没对上（人话）
  nlohmann::json to_json() const;
};

/// 把工作区打成一个包。会话、轨迹、图片、记忆、agent.md 都进去。
BundleExport export_workspace_bundle(const Workspace& ws,
                                     const BundleLimits& limits = BundleLimits());

/// 把一个包还原到工作区里。
///
/// 冲突一律**保留本地**：已经存在的主题、轨迹、记忆条目都不动，报告里说清留了
/// 哪些。返回 false 时 `err` 说明这个包根本读不了（不是 zip、清单不对）。
bool import_workspace_bundle(Workspace& ws, const std::string& zip_bytes,
                             BundleImport* report, std::string* err);

}  // namespace tea
