# aln-data-uploader

ALN 谐振器数据平台的**同事侧上传工具**：在本机把 snp 压缩包提取成参数分享包，
一键提交后网站（<https://bzjing925.github.io/aln-data-web/>）自动更新数据批次。

## 下载安装

到 [Releases](../../releases) 下载对应系统：

- macOS（Apple 芯片）：`aln-uploader-macos-arm64.zip` → 解压得 `ALN-Uploader.app`
  - 首次打开如提示"无法验证开发者"：右键 → 打开；或终端执行
    `xattr -d com.apple.quarantine /path/to/ALN-Uploader.app`
- Windows：`aln-uploader-windows-amd64.zip` → 解压得 `ALN-Uploader.exe`，双击运行

运行后浏览器自动打开上传页面（http://127.0.0.1:8630），关闭页面右上角"退出"即结束。

## 使用

1. **添加数据文件**：snp 压缩包（.zip）或散文件（.s1p/.s2p），可多个
2. **批次号**：默认取首个 zip 文件名；与网站现有批次不能重复（在线时自动查重）
3. **对照表**：默认列出让网站已有的对照表（选中即用，无需本地文件）；
   新对照表选"本地 xlsx/csv 文件"
4. 频率范围可空（全频段）；去嵌需要 zip 内含 OPEN/SHORT 校准件
5. 开始打包 → 生成 `分享包_<批次>.zip`
6. 提交：填入 GitHub token 一键创建 PR；没有 token 就把分享包发给管理员代传

## 开发者

- 源码由主仓库 `backend/scripts/dist_uploader.py` vendor 生成，勿直接改 `src/`
- 本机构建：`python build_uploader.py`（macOS 构建 mac 包，Windows 构建 Win 包）
- 发版：打 tag `v*` 推送，CI 自动构建双平台安装包并发 Release
