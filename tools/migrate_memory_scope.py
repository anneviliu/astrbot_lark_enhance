#!/usr/bin/env python3
"""迁移脚本：将个人记忆从「按群隔离」迁移到「按用户隔离」。

将每个群文件（oc_*.json）中 users[open_id].memories[] 迁移到对应的
用户文件（ou_*.json），群文件仅保留 group_memories[]。

适用场景：从 memory_scope="group" 切换到 "user" 时使用。

用法：
    python3 tools/migrate_memory_scope.py <data_dir> [--dry-run] [--backup]

参数：
    data_dir      user_memory 目录路径
    --dry-run     只打印迁移计划，不实际修改文件
    --backup       迁移前备份群文件为 *.bak

示例：
    python3 tools/migrate_memory_scope.py \
        ~/.astrbot/data/plugin_data/astrbot_plugin_lark_enhance/user_memory \
        --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import uuid


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    data["updated_at"] = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def migrate(data_dir: str, dry_run: bool = False, backup: bool = False) -> None:
    user_memory_dir = os.path.join(data_dir, "user_memory")
    if not os.path.isdir(user_memory_dir):
        print(f"[ERROR] user_memory 目录不存在: {user_memory_dir}")
        return

    migrated_users = 0
    migrated_memories = 0
    skipped_files = 0

    for fname in sorted(os.listdir(user_memory_dir)):
        if not fname.endswith(".json"):
            continue
        if fname.endswith(".bak"):
            continue

        # 跳过已经是用户文件（文件名是 open_id = ou_ 开头）
        # 但实际上无法仅凭文件名判断（群 id 也可能不以 oc_ 开头），
        # 所以统一检查文件内容是否有 users 且非空
        fpath = os.path.join(user_memory_dir, fname)
        try:
            data = load_json(fpath)
        except Exception as e:
            print(f"  [SKIP] {fname}: 无法解析 ({e})")
            skipped_files += 1
            continue

        users = data.get("users", {})
        if not users:
            # 没有用户记忆，跳过
            continue

        print(f"\n[FILE] {fname}")
        total_mems = 0

        for user_id, user_data in users.items():
            mems = user_data.get("memories", [])
            if not mems:
                continue

            user_file = os.path.join(user_memory_dir, f"{user_id}.json")

            print(f"  -> {user_id}: {len(mems)} 条记忆", end="")

            if dry_run:
                print(" (dry-run, 跳过)")
                total_mems += len(mems)
                continue

            # 合并到用户文件
            if os.path.exists(user_file):
                ud = load_json(user_file)
                ud.setdefault("users", {}).setdefault(user_id, {}).setdefault(
                    "memories", []
                ).extend(mems)
            else:
                ud = {
                    "group_id": user_id,
                    "users": {user_id: {"memories": list(mems)}},
                    "group_memories": [],
                }
            save_json(user_file, ud)
            print(f" -> {user_file}")
            migrated_users += 1
            migrated_memories += len(mems)
            total_mems += len(mems)

        if total_mems > 0 and not dry_run:
            # 备份群文件
            if backup:
                bak_path = fpath + ".bak"
                shutil.copy2(fpath, bak_path)
                print(f"  [BACKUP] -> {bak_path}")

            # 清空群文件的 users
            data["users"] = {}
            save_json(fpath, data)
            print(f"  [CLEARED] users from {fname}")

    print(f"\n{'=' * 40}")
    if dry_run:
        print(f"[DRY-RUN] 计划迁移 {migrated_memories} 条记忆，涉及文件见上")
    else:
        print(
            f"迁移完成: {migrated_users} 个用户, "
            f"{migrated_memories} 条记忆, "
            f"跳过 {skipped_files} 个文件"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将个人记忆从按群隔离迁移到按用户隔离"
    )
    parser.add_argument(
        "data_dir",
        help="插件 data 目录路径（包含 user_memory/ 子目录的父目录）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印迁移计划，不实际修改文件",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="迁移前备份群文件为 .bak",
    )
    args = parser.parse_args()

    print(f"数据目录: {args.data_dir}")
    print(f"Dry-run: {args.dry_run}")
    print(f"Backup: {args.backup}")

    migrate(args.data_dir, dry_run=args.dry_run, backup=args.backup)


if __name__ == "__main__":
    main()
