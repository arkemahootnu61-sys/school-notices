#!/usr/bin/env python3
"""Campus notice radar. Run once on demand; never schedules a background task."""
from __future__ import annotations

import argparse
from concurrent.futures import CancelledError
import json
from pathlib import Path
import sqlite3
import sys
import webbrowser

ROOT = Path(__file__).resolve().parent


def source_command(args, path, data_dir):
    from radar import config as configuration
    from radar.storage import app_lock
    action = args.source_action
    if action == 'list':
        config = configuration.validate_config(configuration.json_read(path))
        for source in config['sources']:
            print(f'{"[开]" if source.get("enabled",True) else "[关]"} {source["id"]:5} {source["name"]}  {source["url"]}')
        return
    # Reload under the same lock as collection/login and the Web source editor.
    with app_lock(data_dir):
        config = configuration.validate_config(configuration.json_read(path))
        if action == 'import':
            config = configuration.validate_config(configuration.json_read(args.file))
        elif action == 'add':
            config = configuration.add_source(config, args.name, args.url)
        else:
            ids = set(args.ids)
            known = {source['id'] for source in config['sources']}
            if ids - known:
                raise ValueError('未知来源编号：'+', '.join(sorted(ids-known)))
            if action == 'remove':
                config['sources'] = [source for source in config['sources'] if source['id'] not in ids]
            else:
                for source in config['sources']:
                    if source['id'] in ids:
                        source['enabled'] = action == 'enable'
        configuration.save_config(path, config)
    if action == 'import':
        print('已导入来源清单；旧配置已备份。没有导入任何登录凭据。')
    else:
        print('来源配置已更新，旧文件已备份。')


def login(args, data_dir):
    from radar.config import json_read, session_domains, validate_config
    from radar.runner import ManualLoginSession
    config = validate_config(json_read(args.config))
    session = ManualLoginSession(data_dir, direct=args.direct, domains=session_domains(config))
    try:
        session.open(args.url or config['sources'][0]['url'])
        print('网络方式：'+('直连（不使用系统代理或 7890）' if args.direct else '使用系统代理（显式选择）'))
        print('请在专用 Chrome 中亲自登录并处理验证码，也可访问需要的其他学校站点。')
        print('请不要发送密码、Cookie 或专用浏览器资料；普通 Chrome 的会话不会自动共享到这里。')
        input('不要提前关浏览器。完成后回到此窗口按 Enter 保存并关闭…')
        session.finish()
        print('会话已保存；请手动开始采集。服务端策略可能使会话过期，不保证所有站点认证成功。')
    finally:
        session.cancel()


def positive(value):
    n = int(value)
    if n < 1: raise argparse.ArgumentTypeError('必须为正整数')
    return n


def nonnegative(value):
    n = int(value)
    if n < 0: raise argparse.ArgumentTypeError('不能小于 0')
    return n


def port_number(value):
    n = int(value)
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError('端口必须在 0 到 65535 之间')
    return n


def add_network_flags(command):
    flags = command.add_mutually_exclusive_group()
    flags.add_argument('--direct', dest='direct', action='store_true',
                       help='直连，不使用系统代理（默认）；HTTP 和专用 Chrome 均适用')
    flags.add_argument('--system-proxy', dest='direct', action='store_false',
                       help='显式使用系统代理；只有确实需要时才开启')
    command.set_defaults(direct=True)


def add_execution_flags(command):
    command.add_argument('--delay', type=float, default=1, help='同一主机请求最小间隔，至少 0.2 秒')
    command.add_argument('--timeout', type=positive, default=15)
    add_network_flags(command)
    command.add_argument('--output', type=Path, default=ROOT/'output')
    command.add_argument('--no-open', action='store_true')


def parser():
    p = argparse.ArgumentParser(description='校园通知雷达：手动采集一次，或打开本地控制台。')
    p.add_argument('--config', type=Path, default=ROOT/'sources.json', help='来源清单 JSON')
    p.add_argument('--data-dir', type=Path, help='私有状态目录；默认在当前用户本机应用数据目录')
    commands = p.add_subparsers(dest='command')
    commands.add_parser('init', help='生成默认来源配置，不覆盖已有配置')
    auth = commands.add_parser('login', help='打开专用 Chrome，让本人手动登录')
    auth.add_argument('url', nargs='?', default=None,
                      help='登录入口；默认使用来源配置中的第一个来源网址')
    add_network_flags(auth)
    run = commands.add_parser('run', help='读取一次通知并生成 HTML 增量汇总')
    modes = run.add_mutually_exclusive_group()
    modes.add_argument('--http', dest='transport_mode', action='store_const', const='http', help='仅使用 HTTP，不创建 Chrome')
    modes.add_argument('--browser', dest='transport_mode', action='store_const', const='browser', help='强制使用专用 Chrome 会话，只读采集')
    run.set_defaults(transport_mode='auto')
    run.add_argument('--only', nargs='+', metavar='ID', help='仅运行这些来源（可以临时选择已关闭来源）')
    run.add_argument('--all', action='store_true', help='运行候选清单中的全部来源，含需登录/待适配/校外来源')
    run.add_argument('--days', type=nonnegative, default=30, help='最近天数，0 为不限；日期未知项单独标示')
    run.add_argument('--pages-per-seed', type=positive, default=2)
    run.add_argument('--max-list-pages-per-source', type=positive, default=16)
    run.add_argument('--max-details-per-source', type=positive, default=25)
    run.add_argument('--max-pages', type=positive, default=200)
    run.add_argument('--max-details', type=positive, default=150)
    run.add_argument('--include-seen', action='store_true', help='也展示已收录且未变化的近期通知')
    run.add_argument('--keyword', action='append', default=[], help='按标题关键词初筛，可重复指定，按任一匹配；不是全文搜索')
    add_execution_flags(run)
    serve = commands.add_parser('serve', help='打开仅绑定本机的手动采集控制台')
    serve.add_argument('--port', type=port_number, default=8765, help='本机端口，0 为系统分配')
    add_execution_flags(serve)
    source = commands.add_parser('sources', help='列出、增删、开关或导入来源')
    sub = source.add_subparsers(dest='source_action', required=True)
    sub.add_parser('list')
    add = sub.add_parser('add'); add.add_argument('name'); add.add_argument('url')
    for action in ('remove','enable','disable'):
        item=sub.add_parser(action); item.add_argument('ids',nargs='+')
    imp = sub.add_parser('import'); imp.add_argument('file',type=Path)
    return p


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(encoding='utf-8')
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(raw_args)
    if args.command is None:
        args = parser().parse_args(raw_args + ['run'])
    try:
        from radar import config as configuration
        from radar.storage import app_lock, default_data_dir
        data_dir = (args.data_dir or default_data_dir()).resolve()
        if not args.config.exists():
            with app_lock(data_dir):
                configuration.make_default(args.config)
        if args.command == 'init':
            configuration.validate_config(configuration.json_read(args.config))
            print('来源配置：'+str(args.config)); return 0
        if args.command == 'sources':
            source_command(args, args.config, data_dir); return 0
        if args.command == 'login':
            login(args, data_dir); return 0
        if args.command == 'serve':
            from radar.web import serve_console
            return serve_console(args.config.resolve(), data_dir, args.output.resolve(), port=args.port,
                                 open_browser=not args.no_open, direct=args.direct,
                                 timeout=args.timeout, delay=args.delay)
        from radar.engine import Options
        from radar.runner import run_collection
        options = Options(days=args.days, pages_per_seed=args.pages_per_seed,
                          max_list_pages_per_source=args.max_list_pages_per_source,
                          max_details_per_source=args.max_details_per_source, max_pages=args.max_pages,
                          max_details=args.max_details, include_seen=args.include_seen,
                          transport_mode=args.transport_mode, keywords=tuple(args.keyword))
        result, paths = run_collection(args.config, data_dir, args.output, options,
                                       source_ids=args.only, all_sources=args.all, direct=args.direct,
                                       timeout=args.timeout, delay=args.delay)
        print(f'完成：展示 {len(result["items"])} 条，未重复展示 {result["seen_omitted"]} 条。请同时查看报告中的失败与覆盖说明。')
        print('网页报告：'+str(paths[0]))
        print('最新报告：'+str(args.output.resolve()/'latest.html'))
        if not args.no_open:
            webbrowser.open(paths[0].as_uri())
        return 2 if result['outcome'] == 'failed' else 0
    except (ImportError, ModuleNotFoundError):
        print('缺少依赖：请先双击 setup.bat 安装，或运行 python -m pip install -r requirements.txt。', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, CancelledError, EOFError):
        print('\n已停止；未完成的本轮不会提交已见状态。已完成的历史报告仍保留。')
        return 130
    except (ValueError, RuntimeError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print('错误：'+str(error),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
