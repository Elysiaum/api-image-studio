# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与
[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-10-05

首个版本。

### 新增

- **API 中转站生图**：支持任意 OpenAI 兼容的图像接口
  - Base URL 智能拼接（自动补 `/v1`，也接受完整接口地址）
  - 参数自动降级重试（服务端不支持 `response_format`、`quality` 等字段时自动移除后重试）
  - `n > 1` 失败时自动降级为逐张请求
  - 兼容多种返回结构（`data[]` / `images[]` / 裸对象，`b64_json` / `url` / data-uri）
- **ComfyUI 生图**：导入 API 格式工作流，自动识别节点映射
  - 顺着 KSampler 的 `positive` / `negative` 连线定位提示词节点
  - 自动识别尺寸、种子、批量、参考图（LoadImage）节点，均可手动改
  - WebSocket 实时采样进度，连不上时自动退回轮询
  - 参考图自动上传到 ComfyUI 的 input 目录
- **图生图 / 垫图**：支持 `/images/edits`（标准）与 `/images/generations + image` 两种调用方式
- **批量生成**：一次最多 10 张
- **完整参数面板**：尺寸（含自定义）、数量、质量、风格、种子、返回格式
- **历史记录**：缩略图回看、一键复用提示词与参数、单条删除、全部清空
- **结果操作**：放大预览、用默认程序打开、在文件夹中显示、全部另存到桌面
- **深色 / 浅色主题**
- **安装包**：单文件安装程序，带安装向导、桌面与开始菜单快捷方式、卸载程序
  - 每用户安装，无需管理员权限
  - 卸载时能干净地删掉自身（Windows 允许重命名运行中的 exe）

### 说明

- 界面基于 CustomTkinter。早期版本曾用 pywebview + Edge WebView2，
  但在部分 Windows 11 环境下 WebView2 的页面导航会偶发不完成（窗口只显示背景色），
  故改用 tkinter 系以保证稳定性。
- 配置与历史记录：源码运行时存放在项目根目录；打包安装后存放在 `%APPDATA%\API生图平台`。
- 默认图片输出：源码运行时为 `output/`；打包安装后为 `我的图片\API生图平台`。
