# 服务器一键更新

生产镜像通过 GitHub 质量检查和容器冒烟检查后，`latest` 才会切换到新版本。
服务器上的更新器会读取镜像中的完整提交编号，并将 `.env` 固定到对应的不可变
`sha-...` 标签，然后执行迁移、更新 Web/Worker/Scheduler 并等待健康检查。

首次安装更新器：

```bash
curl -fsSL https://raw.githubusercontent.com/zfh970745204/sub2api-image-studio/main/deploy/update-server.sh -o /usr/local/sbin/sub2image-update
chmod 755 /usr/local/sbin/sub2image-update
```

以后每次发布完成后只需执行：

```bash
sub2image-update
```

部署目录默认为 `/opt/sub2api-image-studio-release-344e9d5`。如需使用其他目录：

```bash
SUB2IMAGE_DEPLOY_DIR=/opt/another-release sub2image-update
```

更新器不会在服务器构建镜像。下载或配置检查失败时，现有服务保持不变；切换后
健康检查失败时，更新器会恢复原 `.env` 并尝试重新启动上一版本。
