# -*- coding: utf-8 -*-
"""
专利序列分析工具 - Web原型（增强版）
功能：异步任务、实时进度、结果预览、查询历史
"""
import os
import sys
import subprocess
import uuid
import json
import csv
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev-secret-key-2024'
socketio = SocketIO(app, cors_allowed_origins="*")

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
QUERY_SCRIPT = PROJECT_ROOT / "query_patent.py"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
HISTORY_FILE = Path(__file__).parent / "query_history.json"

# 任务存储
tasks = {}
query_history = []

# 加载历史记录
if HISTORY_FILE.exists():
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            query_history = json.load(f)
    except:
        query_history = []


def save_history():
    """保存查询历史"""
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(query_history[-50:], f, ensure_ascii=False, indent=2)  # 只保留最近50条


def run_query_task(task_id, targets, start_date, end_date):
    """后台运行查询任务"""
    target_list = [t.strip() for t in targets.split(',') if t.strip()]
    
    # 更新状态
    tasks[task_id]['status'] = 'running'
    socketio.emit('task_update', {
        'task_id': task_id,
        'status': 'running',
        'message': '开始查询...'
    })
    
    cmd = [
        sys.executable,
        str(QUERY_SCRIPT),
        '--targets'] + target_list + [
        '--start', start_date,
        '--end', end_date,
        '--max-pages', '5',
        '--output-dir', str(OUTPUTS_DIR)
    ]
    
    try:
        # 实时输出进度
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT)
        )
        
        # 读取输出
        output_lines = []
        for line in process.stdout:
            output_lines.append(line.strip())
            # 发送进度更新
            socketio.emit('task_update', {
                'task_id': task_id,
                'status': 'running',
                'message': line.strip()
            })
        
        process.wait()
        
        if process.returncode == 0:
            # 查找生成的CSV文件
            output_files = []
            for target in target_list:
                csv_files = list(OUTPUTS_DIR.glob(f"{target}_patent_sequences_*.csv"))
                if csv_files:
                    latest = sorted(csv_files, reverse=True)[0]
                    output_files.append(latest.name)
            
            tasks[task_id]['status'] = 'completed'
            tasks[task_id]['output_files'] = output_files
            tasks[task_id]['end_time'] = datetime.now().isoformat()
            
            # 添加到历史记录
            query_history.append({
                'task_id': task_id,
                'targets': target_list,
                'start_date': start_date,
                'end_date': end_date,
                'output_files': output_files,
                'timestamp': tasks[task_id]['start_time'],
                'status': 'completed'
            })
            save_history()
            
            socketio.emit('task_update', {
                'task_id': task_id,
                'status': 'completed',
                'output_files': output_files,
                'message': f'✅ 查询完成！生成 {len(output_files)} 个文件'
            })
        else:
            error_msg = process.stderr.read()
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['error'] = error_msg
            
            socketio.emit('task_update', {
                'task_id': task_id,
                'status': 'failed',
                'message': f'❌ 查询失败: {error_msg[:200]}'
            })
    
    except Exception as e:
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['error'] = str(e)
        
        socketio.emit('task_update', {
            'task_id': task_id,
            'status': 'failed',
            'message': f'❌ 异常: {str(e)}'
        })


@app.route('/')
def index():
    """首页"""
    return render_template('index.html')


@app.route('/api/submit', methods=['POST'])
def submit_query():
    """提交查询任务（异步）"""
    data = request.json
    targets = data.get('targets', '').strip()
    start_date = data.get('start_date', '2020-01-01')
    end_date = data.get('end_date', datetime.now().strftime('%Y-%m-%d'))
    
    if not targets:
        return jsonify({'error': '请输入靶点名称'}), 400
    
    # 生成任务ID
    task_id = str(uuid.uuid4())[:8]
    
    # 创建任务记录
    tasks[task_id] = {
        'task_id': task_id,
        'status': 'pending',
        'targets': targets,
        'start_date': start_date,
        'end_date': end_date,
        'start_time': datetime.now().isoformat(),
        'output_files': []
    }
    
    # 启动后台线程
    thread = threading.Thread(
        target=run_query_task,
        args=(task_id, targets, start_date, end_date)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'task_id': task_id,
        'status': 'pending',
        'message': '任务已提交，正在后台执行...'
    })


@app.route('/api/status/<task_id>')
def get_status(task_id):
    """查询任务状态"""
    if task_id not in tasks:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify(tasks[task_id])


@app.route('/api/history')
def get_history():
    """获取查询历史"""
    return jsonify(query_history[-20:][::-1])  # 最近20条，倒序


@app.route('/api/preview/<filename>')
def preview_file(filename):
    """预览CSV文件内容"""
    file_path = OUTPUTS_DIR / filename
    if not file_path.exists():
        return jsonify({'error': '文件不存在'}), 404
    
    try:
        rows = []
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i >= 100:  # 最多预览100行
                    break
                rows.append(row)
        
        return jsonify({
            'filename': filename,
            'rows': rows,
            'total': len(rows)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/download/<filename>')
def download_file(filename):
    """下载结果文件"""
    file_path = OUTPUTS_DIR / filename
    if not file_path.exists():
        return jsonify({'error': '文件不存在'}), 404
    return send_file(file_path, as_attachment=True)


@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    print('客户端已连接')


@socketio.on('disconnect')
def handle_disconnect():
    """客户端断开"""
    print('客户端已断开')


if __name__ == '__main__':
    print("=" * 70)
    print("🧬 专利序列分析工具 - Web原型（增强版）")
    print("=" * 70)
    print(f"✅ 异步任务支持")
    print(f"✅ WebSocket实时进度")
    print(f"✅ 结果预览")
    print(f"✅ 查询历史")
    print(f"✅ 批量查询")
    print("=" * 70)
    print(f"访问地址: http://127.0.0.1:5000")
    print(f"查询脚本: {QUERY_SCRIPT}")
    print(f"输出目录: {OUTPUTS_DIR}")
    print("=" * 70)
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
