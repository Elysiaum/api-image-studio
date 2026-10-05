# 参与贡献

感谢你有兴趣参与！无论是提 Issue、改文档还是写代码，都非常欢迎。

## 提 Issue

- **缺陷**：请用 [Bug 报告](https://github.com/Elysiaum/api-image-studio/issues/new?template=bug_report.yml) 模板，把版本、运行方式、复现步骤填清楚
- **功能建议**：请用 [功能建议](https://github.com/Elysiaum/api-image-studio/issues/new?template=feature_request.yml) 模板，重点说清楚「要解决什么问题」
- 提交前请先搜索是否已有相同 Issue
- **贴日志前务必删掉 API Key、Token 等敏感信息**

## 提 PR

1. Fork 本仓库，从 `main` 切一个分支，名字建议 `fix/xxx` 或 `feat/xxx`
2. 改完先跑测试（见下）
3. 提交 PR，按模板填清楚改动内容

### 开发环境

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
set PYTHONPATH=src
.venv\Scripts\python.exe -m api_image_studio
```

### 跑测试

三套测试都自带**本地模拟服务**，不消耗任何 API 额度，可以随便跑：

```bat
.venv\Scripts\python.exe tests\test_core.py       :: 后端 + 界面冒烟
.venv\Scripts\python.exe tests\test_api_e2e.py    :: 模拟 OpenAI 兼容中转站
.venv\Scripts\python.exe tests\test_comfy.py      :: 模拟 ComfyUI（HTTP + WebSocket）
```

## 代码约定

### 分层

```
src/api_image_studio/
├── gui.py          ← 界面层，可以用 tkinter / CustomTkinter
└── core/           ← 核心层，禁止依赖任何界面库
```

**`core/` 里的代码不许 import tkinter 或 customtkinter。** 这样核心逻辑才能被单独复用、
也才能在没有图形界面的环境里测试。新增后端（比如接入别的生图服务）请放在 `core/`。

### 其它

- 面向用户的文案用中文，标识符用英文
- 注释解释「为什么」，而不是「做了什么」
- 提交信息建议用 `类型: 简述` 的格式，例如 `fix: ComfyUI 进度在队列中不更新`
- 新增依赖前请先说明必要性，优先用标准库

## 关于第三方接口

接入新的生图服务时，请注意：

- 不要硬编码任何 API 地址或密钥
- 报错信息要能让用户看懂，不要直接把原始异常抛给用户
- 服务端的非标准返回结构尽量兼容，而不是直接报错

## 许可

贡献的代码将以本项目的 [MIT 许可证](LICENSE) 发布。
