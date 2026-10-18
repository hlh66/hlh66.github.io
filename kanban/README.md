# 班级看班系统（手机服务器版同步备份）

> 主部署：手机服务器（局域网）http://192.168.123.51:5000/kanban/teacher
> 本仓库是网页与代码的云端备份，方便随时查看和下载。

## 在线页面（GitHub Pages 静态版）

| 页面 | 地址 |
|---|---|
| 个人工作台（完整可用） | https://hlh66.github.io/kanban/workbench.html |
| 教师端（页面预览，数据需连手机服务器） | https://hlh66.github.io/kanban/teacher.html |
| 学生端（同上） | https://hlh66.github.io/kanban/student.html |
| 家长端（同上） | https://hlh66.github.io/kanban/parent.html |

⚠️ 学生端/教师端/家长端的数据接口需要手机服务器（局域网 5000 端口），Pages 上只能看页面框架；**工作台是纯静态的，Pages 上完整可用**。

## 下载

- 教室电脑被控端单文件：`down/pc_agent_web.exe`（首次运行自动生成配置）
- zip 整包：`down/pc_agent.zip`

## 同步方法

改完本地 `D://Desktop//服务器//手机服务器//看班系统//` 后，把 web/*.html、kanban_api.py、说明.md 拷进本目录再推送即可。
