#!/usr/bin/env python
"""
Backpack Exchange 做市交易程序统一入口
支持命令行模式
"""
import argparse
import sys
import os
from typing import Optional
from config import ENABLE_DATABASE
from logger import setup_logger

# 创建记录器
logger = setup_logger("main")

def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='Backpack Exchange 做市交易程序')
    
    # 模式选择
    parser.add_argument('--cli', action='store_true', help='启动命令行界面')
    parser.add_argument('--web', action='store_true', help='启动Web界面')
    
    # 基本参数
    parser.add_argument('--exchange', type=str, choices=['backpack', 'aster', 'paradex', 'lighter'], default='backpack', help='交易所选择 (backpack、aster、paradex 或 lighter)')
    parser.add_argument('--api-key', type=str, help='API Key (可选，默认使用环境变量或配置文件)')
    parser.add_argument('--secret-key', type=str, help='Secret Key (可选，默认使用环境变量或配置文件)')
    parser.add_argument('--ws-proxy', type=str, help='WebSocket Proxy (可选，默认使用环境变量或配置文件)')
    
    # 做市参数
    parser.add_argument('--symbol', type=str, help='交易对 (例如: SOL_USDC)')
    parser.add_argument('--spread', type=float, help='价差百分比 (例如: 0.5)')
    parser.add_argument('--quantity', type=float, help='订单数量 (可选)')
    parser.add_argument('--max-orders', type=int, default=3, help='每侧最大订单数量 (默认: 3)')
    parser.add_argument('--force_adjust_spread', type=float, default=None, help='强制调整价差数值% (例如 0.0005)')
    # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←← 新增：全部成交才重挂的开关 ←←←←←←←←←←←←←←←←←←←←←
    parser.add_argument(
        '--wait-all-filled',
        action='store_true',
        default=True,
        help='只有所有挂单全部成交才重新挂单（推荐、最省手续费，默认开启）'
    )
    parser.add_argument(
        '--no-wait-all-filled',
        dest='wait_all_filled',
        action='store_false',
        help='部分成交就立刻重新挂单（旧逻辑，仅在极端追价场景使用）'
    )
    # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←← 结束新增 ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
    parser.add_argument('--duration', type=int, default=3600, help='运行时间（秒）(默认: 3600)')
    parser.add_argument('--interval', type=int, default=60, help='更新间隔（秒）(默认: 60)')
    parser.add_argument('--market-type', choices=['spot', 'perp'], default='spot', help='市场类型 (spot 或 perp)')
    parser.add_argument('--target-position', type=float, default=1.0, help='永续合约目标持仓量 (绝对值, 例如: 1.0)')
    parser.add_argument('--max-position', type=float, default=1.0, help='永续合约最大允许仓位(绝对值)')
    parser.add_argument('--position-threshold', type=float, default=0.1, help='永续仓位调整触发值')
    parser.add_argument('--inventory-skew', type=float, default=0.0, help='永续仓位偏移调整系数 (0-1)')
    parser.add_argument('--stop-loss', type=float, help='永续仓位止损触发值 (以报价资产计价)')
    parser.add_argument('--take-profit', type=float, help='永续仓位止盈触发值 (以报价资产计价)')
    parser.add_argument('--strategy', choices=['standard', 'maker_hedge', 'grid', 'perp_grid'], default='standard', help='策略选择 (standard, maker_hedge, grid 或 perp_grid)')

    # 网格策略参数
    parser.add_argument('--grid-upper', type=float, help='网格上限价格')
    parser.add_argument('--grid-lower', type=float, help='网格下限价格')
    parser.add_argument('--grid-num', type=int, default=10, help='网格数量 (默认: 10)')
    parser.add_argument('--auto-price', action='store_true', help='自动设置网格价格范围')
    parser.add_argument('--price-range', type=float, default=5.0, help='自动模式下的价格范围百分比 (默认: 5.0)')
    parser.add_argument('--grid-mode', choices=['arithmetic', 'geometric'], default='arithmetic', help='网格模式 (arithmetic 或 geometric)')
    parser.add_argument('--grid-type', choices=['neutral', 'long', 'short'], default='neutral', help='永续网格类型 (neutral, long 或 short)')

    # 数据库选项
    parser.add_argument('--enable-db', dest='enable_db', action='store_true', help='启用数据库写入功能')
    parser.add_argument('--disable-db', dest='enable_db', action='store_false', help='停用数据库写入功能')
    parser.set_defaults(enable_db=ENABLE_DATABASE)
    
    # 重平设置参数
    parser.add_argument('--enable-rebalance', action='store_true', help='开启重平功能')
    parser.add_argument('--disable-rebalance', action='store_true', help='关闭重平功能')
    parser.add_argument('--base-asset-target', type=float, help='基础资产目标比例 (0-100, 默认: 30)')
    parser.add_argument('--rebalance-threshold', type=float, help='重平触发阈值 (>0, 默认: 15)')

    return parser.parse_args()

def validate_rebalance_args(args):
    """验证重平设置参数"""
    if getattr(args, 'market_type', 'spot') == 'perp':
        return
    if args.enable_rebalance and args.disable_rebalance:
        logger.error("不能同时设置 --enable-rebalance 和 --disable-rebalance")
        sys.exit(1)
    
    if args.base_asset_target is not None:
        if not 0 <= args.base_asset_target <= 100:
            logger.error("基础资产目标比例必须在 0-100 之间")
            sys.exit(1)
    
    if args.rebalance_threshold is not None:
        if args.rebalance_threshold <= 0:
            logger.error("重平触发阈值必须大于 0")
            sys.exit(1)

def main():
    """主函数"""
    args = parse_arguments()
    
    # 验证重平参数
    validate_rebalance_args(args)
    
    exchange = args.exchange
    api_key = ''
    secret_key = ''
    account_address: Optional[str] = None
    ws_proxy = None
    exchange_config = {}

    if exchange == 'backpack':
        api_key = os.getenv('BACKPACK_KEY', '')
        secret_key = os.getenv('BACKPACK_SECRET', '')
        ws_proxy = os.getenv('BACKPACK_PROXY_WEBSOCKET')
        base_url = os.getenv('BASE_URL', 'https://api.backpack.exchange')
        exchange_config = {
            'api_key': api_key,
            'secret_key': secret_key,
            'base_url': base_url,
            'api_version': 'v1',
            'default_window': '5000'
        }
    elif exchange == 'aster':
        api_key = os.getenv('ASTER_API_KEY', '')
        secret_key = os.getenv('ASTER_SECRET_KEY', '')
        ws_proxy = os.getenv('ASTER_PROXY_WEBSOCKET')
        exchange_config = {
            'api_key': api_key,
            'secret_key': secret_key,
        }
    elif exchange == 'paradex':
        private_key = os.getenv('PARADEX_PRIVATE_KEY', '')  # StarkNet 私钥
        account_address = os.getenv('PARADEX_ACCOUNT_ADDRESS')  # StarkNet 账户地址
        ws_proxy = os.getenv('PARADEX_PROXY_WEBSOCKET')
        base_url = os.getenv('PARADEX_BASE_URL', 'https://api.prod.paradex.trade/v1')

        secret_key = private_key
        api_key = ''  # Paradex 不需要 API Key

        exchange_config = {
            'private_key': private_key,
            'account_address': account_address,
            'base_url': base_url,
        }
    elif exchange == 'lighter':
        api_key = os.getenv('LIGHTER_PRIVATE_KEY') or os.getenv('LIGHTER_API_KEY')
        secret_key = os.getenv('LIGHTER_SECRET_KEY') or api_key
        ws_proxy = os.getenv('LIGHTER_PROXY_WEBSOCKET') or os.getenv('LIGHTER_WS_PROXY')
        base_url = os.getenv('LIGHTER_BASE_URL')
        account_index = os.getenv('LIGHTER_ACCOUNT_INDEX')
        account_address = os.getenv('LIGHTER_ADDRESS')
        if not account_index and account_address:
            from api.lighter_client import _get_lihgter_account_index
            account_index = _get_lihgter_account_index(account_address)
        api_key_index = os.getenv('LIGHTER_API_KEY_INDEX')
        chain_id = os.getenv('LIGHTER_CHAIN_ID')

        exchange_config = {
            'api_private_key': api_key,
            'account_index': account_index,
            'api_key_index': api_key_index,
            'base_url': base_url,
        }
        if chain_id is not None:
            exchange_config['chain_id'] = chain_id
    else:
        logger.error("不支持的交易所，请选择 'backpack'、'aster'、'paradex' 或 'lighter'")
        sys.exit(1)

    # 检查API密钥
    if exchange == 'paradex':
        if not secret_key or not account_address:
            logger.error("Paradex 需要提供 StarkNet 私钥与账户地址，请确认环境变量已设定")
            sys.exit(1)
    elif exchange == 'lighter':
        if not api_key:
            logger.error("缺少 Lighter 私钥，请使用 --api-key 或环境变量 LIGHTER_PRIVATE_KEY 提供")
            sys.exit(1)
        if not exchange_config.get('account_index'):
            logger.error("缺少 Lighter Account Index，请透过环境变量 LIGHTER_ACCOUNT_INDEX 提供")
            sys.exit(1)
    else:
        if not api_key or not secret_key:
            logger.error("缺少API密钥，请通过命令行参数或环境变量提供")
            sys.exit(1)
    
    # 决定执行模式
    if args.web:
        # 启动Web界面
        try:
            logger.info("启动Web界面...")
            from web.server import run_server
            run_server(host='0.0.0.0', port=5000, debug=False)
        except ImportError as e:
            logger.error(f"启动Web界面时出错: {str(e)}")
            logger.error("请确保已安装Flask和flask-socketio: pip install flask flask-socketio")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Web服务器错误: {str(e)}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    elif args.cli:
        # 启动命令行界面
        try:
            from cli.commands import main_cli
            main_cli(api_key, secret_key, ws_proxy=ws_proxy, enable_database=args.enable_db, exchange=exchange)
        except ImportError as e:
            logger.error(f"启动命令行界面时出错: {str(e)}")
            sys.exit(1)
    elif args.symbol and (args.spread is not None or args.strategy in ['grid', 'perp_grid']):
        # 如果指定了交易对，直接运行策略（做市或网格）
        try:
            from strategies.market_maker import MarketMaker
            from strategies.maker_taker_hedge import MakerTakerHedgeStrategy
            from strategies.perp_market_maker import PerpetualMarketMaker
            from strategies.grid_strategy import GridStrategy
            from strategies.perp_grid_strategy import PerpGridStrategy
            
            # 处理重平设置
            market_type = args.market_type

            strategy_name = args.strategy

            # 网格策略处理
            if strategy_name == 'grid':
                logger.info("启动现货网格交易策略")
                logger.info(f"  网格数量: {args.grid_num}")
                logger.info(f"  网格模式: {args.grid_mode}")
                if args.auto_price:
                    logger.info(f"  自动价格范围: ±{args.price_range}%")
                else:
                    logger.info(f"  价格范围: {args.grid_lower} ~ {args.grid_upper}")

                market_maker = GridStrategy(
                    api_key=api_key,
                    secret_key=secret_key,
                    symbol=args.symbol,
                    grid_upper_price=args.grid_upper,
                    grid_lower_price=args.grid_lower,
                    grid_num=args.grid_num,
                    order_quantity=args.quantity,
                    auto_price_range=args.auto_price,
                    price_range_percent=args.price_range,
                    grid_mode=args.grid_mode,
                    ws_proxy=ws_proxy,
                    exchange=exchange,
                    exchange_config=exchange_config,
                    enable_database=args.enable_db
                )

            elif strategy_name == 'perp_grid':
                logger.info("启动永续合约网格交易策略")
                logger.info(f"  网格数量: {args.grid_num}")
                logger.info(f"  网格模式: {args.grid_mode}")
                logger.info(f"  网格类型: {args.grid_type}")
                logger.info(f"  最大持仓量: {args.max_position}")
                if args.auto_price:
                    logger.info(f"  自动价格范围: ±{args.price_range}%")
                else:
                    logger.info(f"  价格范围: {args.grid_lower} ~ {args.grid_upper}")

                market_maker = PerpGridStrategy(
                    api_key=api_key,
                    secret_key=secret_key,
                    symbol=args.symbol,
                    grid_upper_price=args.grid_upper,
                    grid_lower_price=args.grid_lower,
                    grid_num=args.grid_num,
                    order_quantity=args.quantity,
                    auto_price_range=args.auto_price,
                    price_range_percent=args.price_range,
                    grid_mode=args.grid_mode,
                    grid_type=args.grid_type,
                    target_position=args.target_position,
                    max_position=args.max_position,
                    position_threshold=args.position_threshold,
                    inventory_skew=args.inventory_skew,
                    stop_loss=args.stop_loss,
                    take_profit=args.take_profit,
                    ws_proxy=ws_proxy,
                    exchange=exchange,
                    exchange_config=exchange_config,
                    enable_database=args.enable_db
                )

                if args.stop_loss is not None:
                    logger.info(f"  止损阈值: {args.stop_loss} {market_maker.quote_asset}")
                if args.take_profit is not None:
                    logger.info(f"  止盈阈值: {args.take_profit} {market_maker.quote_asset}")

            elif market_type == 'perp':
                logger.info(f"启动永续合约做市模式 (策略: {strategy_name}, 交易所: {exchange})")
                logger.info(f"  目标持仓量: {abs(args.target_position)}")
                logger.info(f"  最大持仓量: {args.max_position}")
                logger.info(f"  仓位触发值: {args.position_threshold}")
                logger.info(f"  报价偏移系数: {args.inventory_skew}")

                if strategy_name == 'maker_hedge':
                    market_maker = MakerTakerHedgeStrategy(
                        api_key=api_key,
                        secret_key=secret_key,
                        symbol=args.symbol,
                        base_spread_percentage=args.spread,
                        order_quantity=args.quantity,
                        target_position=args.target_position,
                        max_position=args.max_position,
                        position_threshold=args.position_threshold,
                        inventory_skew=args.inventory_skew,
                        stop_loss=args.stop_loss,
                        take_profit=args.take_profit,
                        ws_proxy=ws_proxy,
                        exchange=exchange,
                        exchange_config=exchange_config,
                        enable_database=args.enable_db,
                        market_type='perp'
                    )
                else:
                    market_maker = PerpetualMarketMaker(
                        api_key=api_key,
                        secret_key=secret_key,
                        symbol=args.symbol,
                        base_spread_percentage=args.spread,
                        order_quantity=args.quantity,
                        max_orders=args.max_orders,
                        target_position=args.target_position,
                        max_position=args.max_position,
                        position_threshold=args.position_threshold,
                        inventory_skew=args.inventory_skew,
                        stop_loss=args.stop_loss,
                        take_profit=args.take_profit,
                        ws_proxy=ws_proxy,
                        exchange=exchange,
                        exchange_config=exchange_config,
                        wait_all_filled=args.wait_all_filled,
                        force_adjust_spread=args.force_adjust_spread  # [新增] 传入命令行获取的数值
                        enable_database=args.enable_db
                    )

                if args.stop_loss is not None:
                    logger.info(f"  止损阈值: {args.stop_loss} {market_maker.quote_asset}")
                if args.take_profit is not None:
                    logger.info(f"  止盈阈值: {args.take_profit} {market_maker.quote_asset}")
            else:
                if strategy_name == 'maker_hedge':
                    logger.info("启动 Maker-Taker 对冲现货模式")
                    market_maker = MakerTakerHedgeStrategy(
                        api_key=api_key,
                        secret_key=secret_key,
                        symbol=args.symbol,
                        base_spread_percentage=args.spread,
                        order_quantity=args.quantity,
                        ws_proxy=ws_proxy,
                        exchange=exchange,
                        exchange_config=exchange_config,
                        enable_database=args.enable_db,
                        market_type='spot'
                    )
                else:
                    logger.info("启动现货做市模式")
                    enable_rebalance = True  # 默认开启
                    base_asset_target_percentage = 30.0  # 默认30%
                    rebalance_threshold = 15.0  # 默认15%

                    if args.disable_rebalance:
                        enable_rebalance = False
                    elif args.enable_rebalance:
                        enable_rebalance = True

                    if args.base_asset_target is not None:
                        base_asset_target_percentage = args.base_asset_target

                    if args.rebalance_threshold is not None:
                        rebalance_threshold = args.rebalance_threshold

                    logger.info(f"重平设置:")
                    logger.info(f"  重平功能: {'开启' if enable_rebalance else '关闭'}")
                    if enable_rebalance:
                        quote_asset_target_percentage = 100.0 - base_asset_target_percentage
                        logger.info(f"  目标比例: {base_asset_target_percentage}% 基础资产 / {quote_asset_target_percentage}% 报价资产")
                        logger.info(f"  触发阈值: {rebalance_threshold}%")

                    market_maker = MarketMaker(
                        api_key=api_key,
                        secret_key=secret_key,
                        symbol=args.symbol,
                        base_spread_percentage=args.spread,
                        order_quantity=args.quantity,
                        max_orders=args.max_orders,
                        enable_rebalance=enable_rebalance,
                        base_asset_target_percentage=base_asset_target_percentage,
                        rebalance_threshold=rebalance_threshold,
                        ws_proxy=ws_proxy,
                        exchange=exchange,
                        exchange_config=exchange_config,
                        wait_all_filled=args.wait_all_filled,
                        force_adjust_spread=args.force_adjust_spread  # [新增] 传入命令行获取的数值
                        enable_database=args.enable_db
                    )
            
            # 执行做市策略
            market_maker.run(duration_seconds=args.duration, interval_seconds=args.interval)
            
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在退出...")
        except Exception as e:
            logger.error(f"做市过程中发生错误: {e}")
            import traceback
            traceback.print_exc()
    else:
        # 没有指定执行模式时显示帮助
        print("请指定执行模式：")
        print("  --web     启动Web界面")
        print("  --cli     启动命令行界面")
        print("  直接指定  --symbol 和 --spread 参数运行做市策略")
        print("\n支持的交易所：")
        print("  backpack  Backpack 交易所 (默认)")
        print("  aster     Aster 永续合约交易所")
        print("  paradex   Paradex 永续合约交易所")
        print("  lighter   Lighter 永续合约交易所")
        print("\n数据库参数：")
        print("  --enable-db            启用数据库写入")
        print("  --disable-db           停用数据库写入 (预设)")
        print("\n重平设置参数：")
        print("  --enable-rebalance        开启重平功能")
        print("  --disable-rebalance       关闭重平功能")
        print("  --base-asset-target 30    设置基础资产目标比例为30%")
        print("  --rebalance-threshold 15  设置重平触发阈值为15%")
        print("\n=== 范例：现货做市 ===")
        print("  # Backpack 现货做市")
        print("  python run.py --exchange backpack --symbol SOL_USDC --spread 0.5")
        print("\n=== 范例：现货网格 ===")
        print("  # Backpack 现货网格（自动价格范围）")
        print("  python run.py --exchange backpack --symbol SOL_USDC --strategy grid --auto-price --grid-num 10")
        print("  # Backpack 现货网格（手动设定价格范围）")
        print("  python run.py --exchange backpack --symbol SOL_USDC --strategy grid --grid-lower 140 --grid-upper 160 --grid-num 10")
        print("\n=== 范例：永续合约做市 ===")
        print("  # Aster 永续合约做市")
        print("  python run.py --exchange aster --symbol BTCUSDT --spread 0.3 --market-type perp --max-position 0.5")
        print("  # Paradex 永续合约做市")
        print("  python run.py --exchange paradex --symbol BTC-USD-PERP --spread 0.3 --market-type perp --max-position 0.5")
        print("  # Lighter 永续合约做市")
        print("  python run.py --exchange lighter --symbol BTCUSDT --spread 0.3 --market-type perp --max-position 0.5")
        print("\n=== 范例：永续合约网格 ===")
        print("  # Aster 永续网格（中性网格，自动价格）")
        print("  python run.py --exchange aster --symbol BTCUSDT --strategy perp_grid --auto-price --grid-num 10 --grid-type neutral")
        print("  # Paradex 永续网格（做多网格）")
        print("  python run.py --exchange paradex --symbol BTC-USD-PERP --strategy perp_grid --grid-lower 45000 --grid-upper 50000 --grid-num 10 --grid-type long")
        print("  # Lighter 永续网格（做空网格）")
        print("  python run.py --exchange lighter --symbol BTCUSDT --strategy perp_grid --grid-lower 45000 --grid-upper 50000 --grid-num 10 --grid-type short")
        print("  # 永续网格（带止损止盈）")
        print("  python run.py --exchange aster --symbol ETHUSDT --strategy perp_grid --auto-price --grid-num 15 --stop-loss 100 --take-profit 200")
        print("\n使用 --help 查看完整帮助")

if __name__ == "__main__":
    main()
