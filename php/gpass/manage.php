<?php
require 'db.php';

// 设备检测函数
function is_mobile() {
    $user_agent = $_SERVER['HTTP_USER_AGENT'] ?? '';
    return preg_match("/(android|webos|iphone|ipad|ipod|blackberry|windows phone)/i", $user_agent);
}

// 处理删除请求
if (isset($_GET['delete'])) {
    $id = intval($_GET['delete']);
    $stmt = $pdo->prepare("DELETE FROM passwords WHERE id = ?");
    $stmt->execute([$id]);
    header("Location: manage.php");
    exit;
}

// 处理更新/添加请求
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $site_name = $_POST['site_name'];
    $url = $_POST['url'];
    $username = $_POST['username'];
    $password = $_POST['password'];
    $note = $_POST['note'];
    $id = isset($_POST['id']) ? intval($_POST['id']) : null;

    if ($id) {
        // 获取旧密码
        $stmt = $pdo->prepare("SELECT password FROM passwords WHERE id = ?");
        $stmt->execute([$id]);
        $old_password = $stmt->fetchColumn();

        // 如果密码发生变化，记录历史
        if ($old_password !== $password) {
            $history_stmt = $pdo->prepare("INSERT INTO password_history (password_id, old_password, new_password) VALUES (?, ?, ?)");
            $history_stmt->execute([$id, $old_password, $password]);
        }

        // 更新密码
        $stmt = $pdo->prepare("UPDATE passwords SET site_name = ?, url = ?, username = ?, password = ?, note = ? WHERE id = ?");
        $stmt->execute([$site_name, $url, $username, $password, $note, $id]);
    } else {
        // 添加新记录
        $stmt = $pdo->prepare("INSERT INTO passwords (site_name, url, username, password, note) VALUES (?, ?, ?, ?, ?)");
        $stmt->execute([$site_name, $url, $username, $password, $note]);
    }

    header("Location: manage.php");
    exit;
}

// 搜索功能
$search = isset($_GET['search']) ? $_GET['search'] : '';

// 验证10秒内重复提交
if ($search) {
    // 检查数据库是否已经记录了相同的搜索内容
    $stmt = $pdo->prepare("SELECT * FROM search_history WHERE search_query = ? AND TIMESTAMPDIFF(SECOND, search_time, NOW()) < 10");
    $stmt->execute([$search]);
    $existingSearch = $stmt->fetch(PDO::FETCH_ASSOC);

    if (!$existingSearch) {
        // 如果没有重复搜索，记录搜索历史
        $search_stmt = $pdo->prepare("INSERT INTO search_history (search_query, search_time) VALUES (?, NOW())");
        $search_stmt->execute([$search]);
    } else {
        // 提示用户不能重复搜索
        echo "<script>alert('您在10秒内已经搜索过该内容，请稍后再试');</script>";
    }
}

$query = "SELECT * FROM passwords WHERE site_name LIKE ? OR url LIKE ? OR username LIKE ? ORDER BY created_at DESC";
$stmt = $pdo->prepare($query);
$stmt->execute(["%$search%", "%$search%", "%$search%"]);
$passwords = $stmt->fetchAll(PDO::FETCH_ASSOC);
?>

<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>密码管理器 - 管理</title>
    <link href="/style/bootstrap/5.3.2/css/bootstrap.min.css" rel="stylesheet">
    <style>
        #formContainer { transition: all 0.3s ease; }
        .mobile-card { box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 1rem; }
        .mobile-card .btn { margin-top: 0.5rem; }
        .text-break { word-break: break-all; }
        @media (max-width: 768px) {
            .pc-view { display: none !important; }
            .mobile-view { display: block !important; }
        }
        @media (min-width: 769px) {
            .mobile-view { display: none !important; }
        }
    </style>
</head>
<body>
    <div class="container mt-3 mt-md-5">
        <h1 class="text-center mb-4">密码管理器</h1>

        <!-- 搜索框 -->
        <form class="d-flex mb-3" method="get" action="">
            <input type="text" class="form-control" name="search" value="<?= htmlspecialchars($search) ?>" placeholder="搜索站点、URL或用户名" />
            <button class="btn btn-primary ms-2" type="submit">搜索</button>
        </form>

        <button id="toggleFormBtn" class="btn btn-primary mb-3">展开/折叠表单</button>

        <div id="formContainer" style="display: none;">
            <div class="card mb-4">
                <div class="card-header bg-primary text-white">
                    <h5 class="mb-0">添加或编辑密码</h5>
                </div>
                <div class="card-body">
                    <form method="post" id="passwordForm">
                        <input type="hidden" name="id" id="id">
                        <div class="mb-3">
                            <label for="site_name" class="form-label">站点名称</label>
                            <input type="text" class="form-control" name="site_name" id="site_name" required>
                        </div>
                        <div class="mb-3">
                            <label for="url" class="form-label">网站 URL</label>
                            <input type="url" class="form-control" name="url" id="url">
                        </div>
                        <div class="mb-3">
                            <label for="username" class="form-label">用户名</label>
                            <input type="text" class="form-control" name="username" id="username">
                        </div>
                        <div class="mb-3">
                            <label for="password" class="form-label">密码</label>
                            <input type="text" class="form-control" name="password" id="password">
                        </div>
                        <div class="mb-3">
                            <label for="note" class="form-label">备注</label>
                            <textarea class="form-control" name="note" id="note" rows="3"></textarea>
                        </div>
                        <button type="submit" class="btn btn-success">保存</button>
                        <button type="button" class="btn btn-secondary" onclick="clearForm()">清空</button>
                    </form>
                </div>
            </div>
        </div>

        <!-- 移动端布局 -->
        <div class="mobile-view">
            <div class="row">
                <?php foreach ($passwords as $p): ?>
                    <div class="col-12 mb-3">
                        <div class="card mobile-card">
                            <div class="card-body">
                                <h5 class="card-title"><?= htmlspecialchars($p['site_name']) ?></h5>
                                <p class="card-text">
                                    <a href="<?= htmlspecialchars($p['url']) ?>" target="_blank" class="text-break">
                                        <?= htmlspecialchars($p['url']) ?>
                                    </a>
                                </p>
                                <div class="row">
                                    <div class="col-6">
                                        <small class="text-muted">用户名</small>
                                        <p><?= htmlspecialchars($p['username']) ?></p>
                                    </div>
                                    <div class="col-6">
                                        <small class="text-muted">密码</small>
                                        <p><?= htmlspecialchars($p['password']) ?></p>
                                    </div>
                                </div>
                                <?php if (!empty($p['note'])): ?>
                                    <small class="text-muted">备注</small>
                                    <p class="text-break"><?= htmlspecialchars($p['note']) ?></p>
                                <?php endif; ?>
                                <div class="d-grid gap-2">
                                    <button class="btn btn-warning btn-sm" onclick="edit(<?= htmlspecialchars(json_encode($p)) ?>)">编辑</button>
                                    <a href="?delete=<?= $p['id'] ?>" class="btn btn-danger btn-sm" onclick="return confirm('确定删除?')">删除</a>
                                    <button class="btn btn-info btn-sm" onclick="showHistory(<?= $p['id'] ?>)">历史记录</button>
                                </div>
                            </div>
                        </div>
                    </div>
                <?php endforeach; ?>
            </div>
        </div>

        <!-- PC端布局 -->
        <div class="pc-view table-responsive <?= is_mobile() ? 'd-none' : '' ?>">
            <table class="table table-striped table-bordered">
                <thead class="table-dark">
                    <tr>
                        <th>ID</th>
                        <th>站点名称</th>
                        <th>网站 URL</th>
                        <th>用户名</th>
                        <th>密码</th>
                        <th>备注</th>
                        <th>操作</th>
                        <th>历史记录</th>
                    </tr>
                </thead>
                <tbody>
                    <?php foreach ($passwords as $p): ?>
                        <tr>
                            <td><?= $p['id'] ?></td>
                            <td><?= htmlspecialchars($p['site_name']) ?></td>
                            <td>
                                <a href="<?= htmlspecialchars($p['url']) ?>" target="_blank" class="text-break">
                                    <?= htmlspecialchars($p['url']) ?>
                                </a>
                            </td>
                            <td><?= htmlspecialchars($p['username']) ?></td>
                            <td><?= htmlspecialchars($p['password']) ?></td>
                            <td class="text-break"><?= htmlspecialchars($p['note']) ?></td>
                            <td>
                                <button class="btn btn-warning btn-sm" onclick="edit(<?= htmlspecialchars(json_encode($p)) ?>)">编辑</button>
                                <a href="?delete=<?= $p['id'] ?>" class="btn btn-danger btn-sm" onclick="return confirm('确定删除?')">删除</a>
                            </td>
                            <td>
                                <button class="btn btn-info btn-sm" onclick="showHistory(<?= $p['id'] ?>)">查看</button>
                            </td>
                        </tr>
                    <?php endforeach; ?>
                </tbody>
            </table>
        </div>

        <!-- 历史记录模态框 -->
        <div class="modal fade" id="historyModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">密码历史记录</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <table class="table table-bordered">
                            <thead>
                                <tr>
                                    <th>旧密码</th>
                                    <th>新密码</th>
                                    <th>修改时间</th>
                                </tr>
                            </thead>
                            <tbody id="historyBody">
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <style>
        .modal-dialog {
            max-width: 600px;
            width: 100%;
        }
    </style>

    <script src="/style/bootstrap/5.3.2/js/bootstrap.bundle.min.js"></script>
<script>
    // 表单状态管理
    document.addEventListener('DOMContentLoaded', function () {
        // 从本地存储获取表单的状态（展开或收起）
        const formState = localStorage.getItem('formState');
        const formContainer = document.getElementById('formContainer');
        
        // 根据本地存储的状态显示或隐藏表单
        formContainer.style.display = formState === 'expanded' ? 'block' : 'none';

        // 给切换按钮绑定点击事件，切换表单的显示状态
        document.getElementById('toggleFormBtn').addEventListener('click', function () {
            // 切换表单显示状态
            formContainer.style.display = formContainer.style.display === 'none' ? 'block' : 'none';
            // 更新本地存储中的表单状态
            localStorage.setItem('formState', formContainer.style.display === 'block' ? 'expanded' : 'collapsed');
        });
    });
    
    // 编辑功能
    function edit(data) {
        // 根据传入的数据填充表单
        document.getElementById('id').value = data.id;
        document.getElementById('site_name').value = data.site_name;
        document.getElementById('url').value = data.url;
        document.getElementById('username').value = data.username;
        document.getElementById('password').value = data.password;
        document.getElementById('note').value = data.note;
        
        // 显示表单并更新本地存储为展开状态
        document.getElementById('formContainer').style.display = 'block';
        localStorage.setItem('formState', 'expanded');
    }

    // 清空表单
    function clearForm() {
        // 重置表单内容
        document.getElementById('passwordForm').reset();
        // 清空隐藏的 ID 字段
        document.getElementById('id').value = '';
    }

    // 显示历史记录
    async function showHistory(id) {
        try {
            // 发送请求获取历史记录数据
            const response = await fetch(`get_history.php?id=${id}`);
            const data = await response.json();
            
            // 获取表格的 tbody 元素
            const tbody = document.getElementById('historyBody');
            // 使用 map 方法将历史记录数据填充到表格中
            tbody.innerHTML = data.map(item => `
                <tr>
                    <td>${item.old_password}</td>
                    <td>${item.new_password}</td>
                    <td>${item.updated_at}</td>
                </tr>
            `).join('');

            // 使用 Bootstrap 模态框展示历史记录
            new bootstrap.Modal('#historyModal').show();
        } catch (error) {
            console.error('Error:', error);
            alert('获取历史记录失败');
        }
    }

    // 搜索功能
    document.addEventListener('DOMContentLoaded', function () {
        const searchInput = document.getElementById('searchInput');
        const searchBtn = document.getElementById('searchBtn');
        const searchForm = document.getElementById('searchForm');

        let lastSearchTime = 0; // 上次搜索时间

        // 绑定表单提交事件，进行防止重复搜索
        searchForm.addEventListener('submit', function (e) {
            const searchValue = searchInput.value.trim();
            const now = Date.now();

            // 判断是否为空或是否小于 10 秒内重复搜索
            if (searchValue === "" || now - lastSearchTime < 10000) {
                e.preventDefault(); // 阻止表单提交
                alert('请等待 10 秒后再试');
            } else {
                lastSearchTime = now; // 更新搜索时间
            }
        });
    });
</script>
</body>
</html>