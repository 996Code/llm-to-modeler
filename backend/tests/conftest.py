"""公共 pytest fixtures.

测试组合根:平台服务的 SDK 门面在此装配(生产在 main.py 装配,
两处各自完成依赖注入——依赖倒置的标准做法)。最小 app 测试
(只挂 pack router、不经 main)也能拿到已装配的门面。
"""
import sys
from pathlib import Path

# 让 backend/src 可被 import
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT / "src"))

# 装配 sdk.pack_api 的插件配置读取器工厂
from services.pack_settings import PackSettingsReader
from sdk.pack_api import register_settings_reader

register_settings_reader(lambda pack, store: PackSettingsReader(pack, store))

# 装配 sdk.pack_api 的管理端鉴权实现(pack api 测试的最小 app 依赖它)
from api.admin import require_admin
from sdk.pack_api import register_admin_auth

register_admin_auth(require_admin)
