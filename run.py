#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一入口。

存在的理由：让用户不必记住 `python -m productops.cli` 这种写法。
在仓库根目录直接 `python run.py <子命令>` 即可，且**在任何工作目录下都能跑**。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from productops.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
