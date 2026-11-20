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

    incident_p = sub.add_parser("incident", help="质量事件：登记、解除、查询")
    incident_sub = incident_p.add_subparsers(dest="incident_command", required=True)

    register_p = incident_sub.add_parser("register", help="登记质量事件并生成追溯报告")
    register_p.add_argument("--workspace", required=True)
    register_p.add_argument("--source", required=True)
    register_p.add_argument("--output", required=True)

    release_p = incident_sub.add_parser("release", help="解除指定质量事件")
    release_p.add_argument("--workspace", required=True)
    release_p.add_argument("--incident", required=True)
    release_p.add_argument("--output", required=True)

    query_p = incident_sub.add_parser("status", help="查询质量事件及其隔离影响")
    query_p.add_argument("--workspace", required=True)
    query_p.add_argument("--incident", default=None)

    price_p = sub.add_parser("price", help="成本登记与价格查询")
    price_sub = price_p.add_subparsers(dest="price_command", required=True)

    price_register_p = price_sub.add_parser("register", help="登记或更正一根原材的采购成本")
    price_register_p.add_argument("--workspace", required=True)
    price_register_p.add_argument("--source", required=True)
    price_register_p.add_argument("--output", required=True)

    price_status_p = price_sub.add_parser("status", help="查询当前价格与成本修订版")
    price_status_p.add_argument("--workspace", required=True)

    cost_p = sub.add_parser("cost", help="按当前修订版与最新价格查询完整成本明细")
    cost_p.add_argument("--workspace", required=True)

    close_p = sub.add_parser("close", help="连续关账与关账查询")
    close_sub = close_p.add_subparsers(dest="close_command", required=True)

    close_run_p = close_sub.add_parser("run", help="在当前一致截点执行一次关账")
    close_run_p.add_argument("--workspace", required=True)
    close_run_p.add_argument("--source", required=True)
    close_run_p.add_argument("--output", required=True)

    close_status_p = close_sub.add_parser("status", help="查询已冻结的关账结果")
    close_status_p.add_argument("--workspace", required=True)
    close_status_p.add_argument("--close", default=None)

    args = parser.parse_args()
    try:
        if args.command == "init":
            result = workspace.init_workspace(args.workspace, args.source)
        elif args.command == "plan":
            result = workspace.plan(args.workspace, args.source, args.output)
        elif args.command == "commit":
            result = workspace.commit(args.workspace, args.plan, args.output)
        elif args.command == "incident":
            if args.incident_command == "register":
                result = workspace.register_incident(
                    args.workspace, args.source, args.output)
            elif args.incident_command == "release":
                result = workspace.release_incident(
                    args.workspace, args.incident, args.output)
            else:
                result = workspace.incident_status(args.workspace, args.incident)
        elif args.command == "price":
            if args.price_command == "register":
                result = workspace.register_price(
                    args.workspace, args.source, args.output)
            else:
                result = workspace.price_status(args.workspace)
        elif args.command == "cost":
            result = workspace.cost_query(args.workspace)
        elif args.command == "close":
            if args.close_command == "run":
                result = workspace.close_run(args.workspace, args.source, args.output)
            else:
                result = workspace.close_status(args.workspace, args.close)
        else:
            result = workspace.status(args.workspace, args.job)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"stock-workspace: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
