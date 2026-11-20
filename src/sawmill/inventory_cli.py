import argparse
import json
from . import workspace


def main():
    parser = argparse.ArgumentParser(prog="stock-workspace",
                                     description="跨批次余料库存工作区")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="从初始原材清单建立工作区")
    init_p.add_argument("--source", required=True)
    init_p.add_argument("--workspace", required=True)

    plan_p = sub.add_parser("plan", help="基于当前库存修订版生成批次计划")
    plan_p.add_argument("--workspace", required=True)
    plan_p.add_argument("--source", required=True)
    plan_p.add_argument("--output", required=True)

    commit_p = sub.add_parser("commit", help="提交计划，生效库存变更")
    commit_p.add_argument("--workspace", required=True)
    commit_p.add_argument("--plan", required=True)
    commit_p.add_argument("--output", required=True)

    status_p = sub.add_parser("status", help="查询当前库存或已提交批次结果")
    status_p.add_argument("--workspace", required=True)
    status_p.add_argument("--job", default=None)

    args = parser.parse_args()
    try:
        if args.command == "init":
            result = workspace.init_workspace(args.workspace, args.source)
        elif args.command == "plan":
            result = workspace.plan(args.workspace, args.source, args.output)
        elif args.command == "commit":
            result = workspace.commit(args.workspace, args.plan, args.output)
        else:
            result = workspace.status(args.workspace, args.job)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"stock-workspace: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
