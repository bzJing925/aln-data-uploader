"""算法层 - 纯函数模块。

详见 docs/algorithm-port.md。

子模块：
- touchstone: S2P → S1P 拆分
- deembed: scikit-rf ShortOpen 去嵌封装
- extract: 谐振参数提取（fs/fp/Q/BodeQ/k2eff/mBVD/中间峰）
- mapping: 对照表加载与 Description token 解析
- filename: 文件名解析（mark / coord / port_type 等）
"""
