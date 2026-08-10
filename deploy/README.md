# 服务器部署

在本地项目根目录执行部署：

```bash
./deploy/deploy_to_server.sh
```

默认会同步以下内容：

- 策略控制台后端和静态前端
- 候选币扫描脚本
- 交易策略

默认不会同步交易数据库、运行时参数、行情缓存、日志以及
`user_data/config_scan.json`。这样可以避免日常代码部署覆盖服务器持仓、
控制台参数或 API 凭据。

常用参数：

```bash
# 仅预览将要同步的文件，不修改服务器
./deploy/deploy_to_server.sh --dry-run

# 备份服务器配置后，再上传本地 config_scan.json
./deploy/deploy_to_server.sh --config

# requirements 或依赖发生变化时，重新安装服务器依赖
./deploy/deploy_to_server.sh --deps

# 修改了 Freqtrade 框架源码时同步核心代码
./deploy/deploy_to_server.sh --core

# 应用修改后的 systemd 或 Nginx 配置
./deploy/deploy_to_server.sh --systemd
./deploy/deploy_to_server.sh --nginx

# 只同步文件，不重启服务
./deploy/deploy_to_server.sh --no-restart
```

脚本会保留机器人的运行状态：如果部署前服务器机器人处于运行状态，
服务重启后会自动恢复为 `RUNNING`。

脚本采用单次增量同步，并根据文件变化只重启受影响的服务。SSH 连接会
保留 5 分钟，短时间内连续部署可复用连接。
