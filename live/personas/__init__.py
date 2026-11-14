"""人设模板：把人设提示词、触发器表、弹幕意图、称呼、知识库兜底、节目单叫法打包成一个可切换的模板。

  builtin/<id>/  出厂模板（commerce 带货主播 / explainer 讲解·系统介绍 / entertainment 娱乐主播），只读
  models.py      字段默认值、规范化、校验
  store.py       读写模板目录（用户模板在 live_data/personas/<id>/）
  service.py     PersonaService：当前人设 → cfg["persona"]；复制 / 编辑 / 删除；停播时切换 + 全量重置钩子
  migrate.py     一次性迁移旧版本散落在全局设置里的人设
"""
