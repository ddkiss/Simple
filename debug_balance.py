import os
from api.bp_client import BPClient
from dotenv import load_dotenv

load_dotenv()

# 初始化客户端
config = {
    'api_key': os.getenv('BACKPACK_KEY'),
    'secret_key': os.getenv('BACKPACK_SECRET'),
    'base_url': 'https://api.backpack.exchange'
}
client = BPClient(config)

print("正在查询余额...")

# 1. 查询现货余额
spot = client.get_balance()
if 'USDC' in spot:
    print(f"现货 USDC: {spot['USDC']}")
else:
    print("现货账户中未找到 USDC")

# 2. 查询合约抵押品
collateral = client.get_collateral()
print(f"抵押品信息: {collateral}")
