# 专利序列分析工具 - Web原型

**用时**: 20分钟  
**目标**: 把CLI工具变成网页应用，让非技术用户也能使用

## 快速启动

```bash
cd prototype

# 安装依赖
pip install -r requirements.txt

# 启动服务
python app.py

# 打开浏览器访问
# http://127.0.0.1:5000
```

## 功能

✅ 网页表单输入靶点名称  
✅ 后台运行query_patent.py  
✅ 显示查询状态  
✅ 下载生成的CSV文件  

## 技术栈

- **后端**: Flask（极简）
- **前端**: HTML + Alpine.js（无需构建工具）
- **部署**: 单机运行

## 下一步改进

- [ ] 异步任务队列（避免阻塞）
- [ ] 进度实时更新（WebSocket）
- [ ] 结果预览（不用下载就能看）
- [ ] 用户认证
