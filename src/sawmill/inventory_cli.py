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

    delivery_p = sub.add_parser("delivery", help="成品配套交付与质量召回定位")
    delivery_sub = delivery_p.add_subparsers(dest="delivery_command", required=True)

    dplan_p = delivery_sub.add_parser("plan", help="基于当前成品修订版预览整套配货")
    dplan_p.add_argument("--workspace", required=True)
    dplan_p.add_argument("--source", required=True)
    dplan_p.add_argument("--output", required=True)

    dcommit_p = delivery_sub.add_parser("commit", help="提交交付计划，出库所选成品")
    dcommit_p.add_argument("--workspace", required=True)
    dcommit_p.add_argument("--plan", required=True)
    dcommit_p.add_argument("--output", required=True)

    dstatus_p = delivery_sub.add_parser("status", help="查询成品归属与交付召回状态")
    dstatus_p.add_argument("--workspace", required=True)
    dstatus_p.add_argument("--dispatch", default=None)

    daffected_p = delivery_sub.add_parser("affected", help="按质量事件定位历史交付")
    daffected_p.add_argument("--workspace", required=True)
    daffected_p.add_argument("--incident", required=True)

    inspection_p = sub.add_parser("inspection", help="局部检验登记与查询")
    inspection_sub = inspection_p.add_subparsers(
        dest="inspection_command", required=True)

    iregister_p = inspection_sub.add_parser("register", help="登记条目的局部剔除区检验")
    iregister_p.add_argument("--workspace", required=True)
    iregister_p.add_argument("--source", required=True)
    iregister_p.add_argument("--output", required=True)

    istatus_p = inspection_sub.add_parser("status", help="查询局部检验记录")
    istatus_p.add_argument("--workspace", required=True)
    istatus_p.add_argument("--entry", default=None)

    salvage_p = sub.add_parser("salvage", help="局部缺陷回收切割")
    salvage_sub = salvage_p.add_subparsers(dest="salvage_command", required=True)

    splan_p = salvage_sub.add_parser("plan", help="基于当前库存修订版生成回收计划")
    splan_p.add_argument("--workspace", required=True)
    splan_p.add_argument("--source", required=True)
    splan_p.add_argument("--output", required=True)

    scommit_p = salvage_sub.add_parser("commit", help="提交回收计划，生效库存变更")
    scommit_p.add_argument("--workspace", required=True)
    scommit_p.add_argument("--plan", required=True)
    scommit_p.add_argument("--output", required=True)

    return_p = sub.add_parser("return", help="客户退回成品登记与再制造返料查询")
    return_sub = return_p.add_subparsers(dest="return_command", required=True)

    rregister_p = return_sub.add_parser("register", help="登记完整成品退回并一次性转为返料")
    rregister_p.add_argument("--workspace", required=True)
    rregister_p.add_argument("--source", required=True)
    rregister_p.add_argument("--output", required=True)

    rstatus_p = return_sub.add_parser("status", help="查询退回回单与返料当前状态")
    rstatus_p.add_argument("--workspace", required=True)
    rstatus_p.add_argument("--return", dest="return_id", default=None)

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
        elif args.command == "delivery":
            if args.delivery_command == "plan":
                result = workspace.delivery_plan(args.workspace, args.source, args.output)
            elif args.delivery_command == "commit":
                result = workspace.delivery_commit(args.workspace, args.plan, args.output)
            elif args.delivery_command == "status":
                result = workspace.delivery_status(args.workspace, args.dispatch)
            else:
                result = workspace.delivery_affected(args.workspace, args.incident)
        elif args.command == "inspection":
            if args.inspection_command == "register":
                result = workspace.register_inspection(
                    args.workspace, args.source, args.output)
            else:
                result = workspace.inspection_status(args.workspace, args.entry)
        elif args.command == "salvage":
            if args.salvage_command == "plan":
                result = workspace.salvage_plan(
                    args.workspace, args.source, args.output)
            else:
                result = workspace.salvage_commit(
                    args.workspace, args.plan, args.output)
        elif args.command == "return":
            if args.return_command == "register":
                result = workspace.register_return(
                    args.workspace, args.source, args.output)
            else:
                result = workspace.return_status(args.workspace, args.return_id)
        else:
            result = workspace.status(args.workspace, args.job)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"stock-workspace: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
