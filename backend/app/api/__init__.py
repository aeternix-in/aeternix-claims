from .admin import router as admin_router
from .desk import router as desk_router
from .money import router as money_router

routers = [admin_router, desk_router, money_router]
