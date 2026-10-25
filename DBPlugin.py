# DBPlugin.py - merged into config.py
# 从 config 获取 DBPlugin 类
# config 中 _lazy_imports() 不再反向导入本模块，循环已打破
import sys

if 'config' in sys.modules:
    DBPlugin = sys.modules['config'].DBPlugin
else:
    import config
    DBPlugin = config.DBPlugin
