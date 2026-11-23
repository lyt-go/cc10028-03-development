"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import coverage, resource

# 覆盖图路由固定在 /api/layout-coverage 下，必须先于带 /api/{module} 的通用
# 资源路由注册，否则固定路径会被动态路径抢先匹配。
ROUTERS = [coverage, resource]
