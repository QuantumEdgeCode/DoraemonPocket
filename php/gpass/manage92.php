<?php
require 'db.php';

function is_mobile() {
    $user_agent = $_SERVER['HTTP_USER_AGENT'] ?? '';
    return preg_match("/(android|webos|iphone|ipad|ipod|blackberry|windows phone)/i", $user_agent);
}

if (isset($_GET['delete'])) {
    $id = intval($_GET['delete']);
    $stmt = $pdo->prepare("DELETE FROM passwords WHERE id = ?");
    $stmt->execute([$id]);
    header("Location: manage92.php");
    exit;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $site_name = trim($_POST['site_name']);
    $url = trim($_POST['url']);
    $username = trim($_POST['username']);
    $password = trim($_POST['password']);
    $note = trim($_POST['note']);
    $id = isset($_POST['id']) ? intval($_POST['id']) : null;
    if ($id) {
        $stmt = $pdo->prepare("SELECT password FROM passwords WHERE id = ?");
        $stmt->execute([$id]);
        $old_password = $stmt->fetchColumn();
        if ($old_password !== $password) {
            $history_stmt = $pdo->prepare("INSERT INTO password_history (password_id, old_password, new_password) VALUES (?, ?, ?)");
            $history_stmt->execute([$id, $old_password, $password]);
        }
        $stmt = $pdo->prepare("UPDATE passwords SET site_name = ?, url = ?, username = ?, password = ?, note = ? WHERE id = ?");
        $stmt->execute([$site_name, $url, $username, $password, $note, $id]);
    } else {
        $stmt = $pdo->prepare("INSERT INTO passwords (site_name, url, username, password, note) VALUES (?, ?, ?, ?, ?)");
        $stmt->execute([$site_name, $url, $username, $password, $note]);
    }
    header("Location: manage92.php");
    exit;
}

$search = isset($_GET['search']) ? trim($_GET['search']) : '';
$total_search_count = $today_search_count = $results_count = 0;
if ($search) {
    $search_stmt = $pdo->prepare("INSERT INTO search_history (search_query, search_time) VALUES (?, NOW())");
    $search_stmt->execute([$search]);
    $stmt = $pdo->prepare("SELECT COUNT(*) FROM search_history");
    $stmt->execute();
    $total_search_count = $stmt->fetchColumn();
    $stmt = $pdo->prepare("SELECT COUNT(*) FROM search_history WHERE DATE(search_time) = CURDATE()");
    $stmt->execute();
    $today_search_count = $stmt->fetchColumn();
}

$query = "SELECT * FROM passwords WHERE site_name LIKE ? OR url LIKE ? OR username LIKE ? ORDER BY created_at DESC";
$stmt = $pdo->prepare($query);
$stmt->execute(["%$search%", "%$search%", "%$search%"]);
$passwords = $stmt->fetchAll(PDO::FETCH_ASSOC);
$results_count = count($passwords);
?>
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>密码管理器 - 管理</title>
    <link href="/style/bootstrap/5.3.2/css/bootstrap.min.css" rel="stylesheet">
    <link href="/style/bootstrap-icons/1.11.1/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        /* 你原来的全部样式保留不变 */
        #formContainer { transition: all 0.3s ease; }
        .mobile-card { box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 1rem; }
        .mobile-card .btn { margin-top: 0.5rem; margin-bottom: 0.25rem; }
        .text-break { word-break: break-all; overflow-wrap: break-word; }
        .detail-modal .modal-body { max-height: 70vh; overflow-y: auto; }
        .detail-info {
            background: #f8f9fa;
            border-radius: 0.375rem;
            padding: 1rem;
            margin-bottom: 1rem;
            border-left: 4px solid #0d6efd;
        }
        .detail-info h6 {
            margin-bottom: 0.5rem;
            color: #495057;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        .detail-info p { margin-bottom: 0.25rem; word-break: break-all; }
        .detail-url { color: #0d6efd; text-decoration: none; }
        .detail-url:hover { text-decoration: underline; }
        .detail-section { margin-bottom: 1.5rem; }
        .search-container { max-width: 600px; margin: 0 auto 2rem; position: relative; }
        .search-form { display: flex; align-items: center; gap: 0.5rem; }
        .search-input-group { flex-grow: 1; position: relative; }
        .search-input-group .form-control { padding-right: 2.5rem; border-radius: 0.375rem 0 0 0.375rem; }
        .clear-search {
            position: absolute; right: 0.75rem; top: 50%; transform: translateY(-50%);
            background: none; border: none; color: #6c757d; font-size: 1rem;
            cursor: pointer; display: none; z-index: 5;
        }
        .clear-search:hover { color: #dc3545; }
        .search-btn { border-radius: 0 0.375rem 0.375rem 0; }
        .search-stats-bar {
            background: #f8f9fa; border-radius: 0.375rem; padding: 0.75rem 1rem;
            margin-bottom: 1.5rem; border: 1px solid #dee2e6; font-size: 0.9rem;
        }
        .search-stats-bar .row { align-items: center; }
        .search-stats-item { display: flex; align-items: center; gap: 0.5rem; }
        .search-stats-item i { font-size: 1.1rem; width: 16px; }
        .search-stats-number { font-weight: 600; color: #0d6efd; }
        .search-result-info { text-align: center; margin-top: 0.5rem; color: #6c757d; font-size: 0.9rem; }
        .no-results-container {
            text-align: center; padding: 3rem 1rem;
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
            border-radius: 0.75rem; border: 1px dashed #dee2e6; margin: 2rem 0;
        }
        .no-results-icon { font-size: 4rem; color: #6c757d; display: block; margin-bottom: 1rem; opacity: 0.5; }
        .no-results-title { color: #495057; font-size: 1.5rem; font-weight: 600; margin-bottom: 0.5rem; }
        .no-results-text { color: #6c757d; margin-bottom: 2rem; font-size: 1rem; }
        .clear-search-btn {
            display: inline-flex; align-items: center; gap: 0.5rem;
            padding: 0.5rem 1.25rem; background: linear-gradient(135deg, #6c757d 0%, #5a6268 100%);
            color: white; border: none; border-radius: 50px; font-size: 0.9rem; font-weight: 500;
            text-decoration: none; transition: all 0.2s ease; box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .clear-search-btn:hover {
            background: linear-gradient(135deg, #5a6268 0%, #495057 100%);
            color: white; transform: translateY(-1px); box-shadow: 0 4px 8px rgba(0,0,0,0.15); text-decoration: none;
        }
        .clear-search-btn i { font-size: 1rem; }
        .clear-search-btn:active { transform: translateY(0); }
        @media (max-width: 768px) {
            .pc-view { display: none !important; }
            .mobile-view { display: block !important; }
            .mobile-card .btn-group { margin-top: 0.5rem; }
            .mobile-card h5 { font-size: 1.25rem; }
            .search-container { max-width: 100%; padding: 0 1rem; }
            .search-btn { padding: 0.5rem 1rem; }
            .search-stats-bar { margin: 0 1rem 1rem; padding: 0.5rem; }
            .search-stats-item { font-size: 0.85rem; }
            .no-results-container { margin: 1rem; }
            .no-results-icon { font-size: 3rem; }
            .no-results-title { font-size: 1.25rem; }
            .clear-search-btn { padding: 0.6rem 1.5rem; font-size: 1rem; width: 100%; max-width: 200px; }
        }
        @media (min-width: 769px) {
            .mobile-view { display: none !important; }
            .table-container { overflow-x: auto; margin-bottom: 5rem; }
            .table { margin-bottom: 0; }
            .table td, .table th { vertical-align: middle; padding: 0.75rem; }
            .table thead th {
                position: relative;
                padding-left: 3.3rem !important;
                white-space: nowrap;
                font-weight: 600;
            }
            .table thead th .icon {
                position: absolute;
                left: 1rem;
                top: 50%;
                transform: translateY(-50%);
                font-size: 1.15rem;
                opacity: 0.9;
            }
            .table th:nth-child(1) { width: 90px !important; text-align: center; }
            .table th:nth-child(4) { width: 210px !important; min-width: 210px; text-align: center; }
            .action-buttons {
                display: flex;
                gap: 12px;
                justify-content: center;
                flex-wrap: nowrap;
            }
            .btn-action {
                width: 44px;
                height: 44px;
                padding: 0;
                font-size: 1rem;
                border-radius: 12px;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .table td:nth-child(2),
            .table td:nth-child(3) {
                max-width: 350px;
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .table td:nth-child(2):hover,
            .table td:nth-child(3):hover {
                white-space: normal;
                word-break: break-all;
                background: #f8f9fa;
                z-index: 1;
                position: relative;
            }
            .btn-detail { background-color:#17a2b8; border-color:#17a2b8; color:white; }
            .btn-detail:hover { background-color:#138496; border-color:#117a8b; }
            .btn-edit { background-color:#ffc107; border-color:#ffc107; color:#212529; }
            .btn-edit:hover { background-color:#e0a800; border-color:#d39e00; }
            .btn-delete { background-color:#dc3545; border-color:#dc3545; color:white; }
            .btn-delete:hover { background-color:#c82333; border-color:#bd2130; }
            .btn-history { background-color:#0dcaf0; border-color:#0dcaf0; color:white; }
            .btn-history:hover { background-color:#0aa9d4; border-color:#09b0d3; }
        }
        .empty-state { text-align: center; padding: 3rem 1rem; color: #6c757d; }
        .empty-state i { font-size: 3rem; display: block; margin-bottom: 1rem; opacity: 0.5; }
        .toast-container { position: fixed; top: 1rem; right: 1rem; z-index: 1055; }
    </style>
</head>
<body>
<div class="container mt-3 mt-md-5">
    <h1 class="text-center mb-4">密码管理器</h1>

    <!-- 搜索框 -->
    <div class="search-container">
        <form class="search-form" method="get" action="">
            <div class="search-input-group">
                <input type="text" class="form-control" name="search" id="searchInput" value="<?= htmlspecialchars($search) ?>" placeholder="搜索站点、URL或用户名..." />
                <button type="button" class="clear-search" id="clearSearch" title="清除搜索">
                    <i class="bi bi-x-circle"></i>
                </button>
            </div>
            <button class="btn btn-primary search-btn" type="submit">
                <i class="bi bi-search"></i> 搜索
            </button>
        </form>
        <?php if ($search): ?>
            <div class="search-result-info">
                找到 <?= $results_count ?> 条结果 for "<?= htmlspecialchars($search) ?>"
            </div>
        <?php endif; ?>
    </div>

    <?php if ($search): ?>
    <div class="search-stats-bar">
        <div class="row">
            <div class="col-md-4 col-12">
                <div class="search-stats-item">
                    <i class="bi bi-search text-primary"></i>
                    <span>总搜索次数：</span>
                    <span class="search-stats-number"><?= number_format($total_search_count) ?></span>
                </div>
            </div>
            <div class="col-md-4 col-12">
                <div class="search-stats-item">
                    <i class="bi bi-calendar-day text-success"></i>
                    <span>今日搜索：</span>
                    <span class="search-stats-number"><?= number_format($today_search_count) ?></span>
                </div>
            </div>
            <div class="col-md-4 col-12">
                <div class="search-stats-item">
                    <i class="bi bi-list-nested text-info"></i>
                    <span>搜索结果：</span>
                    <span class="search-stats-number"><?= number_format($results_count) ?></span>
                </div>
            </div>
        </div>
    </div>
    <?php endif; ?>

    <button id="toggleFormBtn" class="btn btn-primary mb-3">
        <i class="bi bi-plus-circle"></i> 展开/折叠表单
    </button>

    <div id="formContainer" style="display: none;">
        <div class="card mb-4">
            <div class="card-header bg-primary text-white">
                <h5 class="mb-0"><i class="bi bi-key"></i> 添加或编辑密码</h5>
            </div>
            <div class="card-body">
                <form method="post" id="passwordForm">
                    <input type="hidden" name="id" id="id">
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="site_name" class="form-label">站点名称 <span class="text-danger">*</span></label>
                            <input type="text" class="form-control" name="site_name" id="site_name" required>
                        </div>
                        <div class="col-md-6 mb-3">
                            <label for="url" class="form-label">网站 URL</label>
                            <input type="url" class="form-control" name="url" id="url" placeholder="https://example.com">
                        </div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label for="username" class="form-label">用户名</label>
                            <input type="text" class="form-control" name="username" id="username">
                        </div>
                        <div class="col-md-6 mb-3">
                            <label for="password" class="form-label">密码</label>
                            <input type="text" class="form-control" name="password" id="password">
                        </div>
                    </div>
                    <div class="mb-3">
                        <label for="note" class="form-label">备注</label>
                        <textarea class="form-control" name="note" id="note" rows="3" placeholder="输入备注信息..."></textarea>
                    </div>
                    <button type="submit" class="btn btn-success me-2">
                        <i class="bi bi-check-lg"></i> 保存
                    </button>
                    <button type="button" class="btn btn-secondary" onclick="clearForm()">
                        <i class="bi bi-arrow-clockwise"></i> 清空
                    </button>
                </form>
            </div>
        </div>
    </div>

    <?php if (empty($passwords) && !$search): ?>
        <div class="empty-state">
            <i class="bi bi-key" style="font-size:4rem;opacity:0.5"></i>
            <h3>暂无密码记录</h3>
            <p class="mb-3">点击上方按钮添加您的第一个密码记录</p>
            <button class="btn btn-primary" onclick="clearForm(); document.getElementById('formContainer').style.display='block'; localStorage.setItem('formState', 'expanded');">
                <i class="bi bi-plus-circle"></i> 添加密码
            </button>
        </div>
    <?php elseif (empty($passwords) && $search): ?>
        <div class="no-results-container">
            <i class="bi bi-search no-results-icon"></i>
            <div class="no-results-title">无搜索结果</div>
            <p class="no-results-text">未找到与 "<?= htmlspecialchars($search) ?>" 相关的记录</p>
            <a href="manage92.php" class="clear-search-btn" onclick="clearSearchAndReturn(); return false;">
                <i class="bi bi-arrow-left"></i> 返回所有记录
            </a>
        </div>
    <?php else: ?>
        <!-- 移动端 -->
        <div class="mobile-view">
            <div class="row">
                <?php foreach ($passwords as $p): ?>
                    <div class="col-12 mb-3">
                        <div class="card mobile-card">
                            <div class="card-body">
                                <div class="d-flex justify-content-between align-items-start mb-2">
                                    <h5 class="card-title mb-1 flex-grow-1"><?= htmlspecialchars($p['site_name']) ?></h5>
                                    <span class="badge bg-secondary ms-2"><?= $p['id'] ?></span>
                                </div>
                                <div class="row mt-2">
                                    <div class="col-6">
                                        <small class="text-muted"><i class="bi bi-person"></i> 用户名</small>
                                        <p class="mb-1 fw-semibold"><?= htmlspecialchars($p['username']) ?: '<span class="text-muted">未设置</span>' ?></p>
                                    </div>
                                    <div class="col-6">
                                        <small class="text-muted"><i class="bi bi-lock"></i> 密码</small>
                                        <p class="mb-0 fw-semibold text-danger"><?= htmlspecialchars($p['password']) ?: '<span class="text-muted">未设置</span>' ?></p>
                                    </div>
                                </div>
                                <div class="d-grid gap-2 mt-3">
                                    <div class="btn-group" role="group">
                                        <button class="btn btn-detail flex-fill" onclick="showDetail(<?= htmlspecialchars(json_encode($p)) ?>)">
                                            <i class="bi bi-eye"></i> 详细
                                        </button>
                                        <button class="btn btn-outline-warning flex-fill" onclick="edit(<?= htmlspecialchars(json_encode($p)) ?>)">
                                            <i class="bi bi-pencil"></i> 编辑
                                        </button>
                                        <a href="?delete=<?= $p['id'] ?>" class="btn btn-outline-danger flex-fill" onclick="return confirm('确定删除此记录吗？')">
                                            <i class="bi bi-trash"></i> 删除
                                        </a>
                                        <button class="btn btn-outline-info flex-fill" onclick="showHistory(<?= $p['id'] ?>)">
                                            <i class="bi bi-clock-history"></i> 历史
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                <?php endforeach; ?>
            </div>
        </div>

        <!-- PC端 -->
        <div class="pc-view <?= is_mobile() ? 'd-none' : '' ?>">
            <div class="table-container">
                <table class="table table-striped table-bordered table-hover">
                    <thead class="table-dark">
                        <tr>
                            <th style="width:90px;">ID</th>
                            <th><i class="bi bi-building icon text-primary"></i> 站点名称</th>
                            <th><i class="bi bi-person icon text-success"></i> 用户名</th>
                            <th style="width:210px;"><i class="bi bi-gear icon text-warning"></i> 操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        <?php foreach ($passwords as $p): ?>
                            <tr>
                                <td class="text-center fw-bold"><?= $p['id'] ?></td>
                                <td title="<?= htmlspecialchars($p['site_name']) ?>">
                                    <?= htmlspecialchars($p['site_name']) ?>
                                </td>
                                <td title="<?= htmlspecialchars($p['username']) ?>">
                                    <?= htmlspecialchars($p['username']) ?: '<span class="text-muted">未设置</span>' ?>
                                </td>
                                <td>
                                    <div class="action-buttons">
                                        <button class="btn btn-detail btn-action" onclick="showDetail(<?= htmlspecialchars(json_encode($p)) ?>)" title="查看详情">
                                            <i class="bi bi-eye"></i>
                                        </button>
                                        <button class="btn btn-edit btn-action" onclick="edit(<?= htmlspecialchars(json_encode($p)) ?>)" title="编辑">
                                            <i class="bi bi-pencil"></i>
                                        </button>
                                        <a href="?delete=<?= $p['id'] ?>" class="btn btn-delete btn-action" onclick="return confirm('确定删除此记录吗？')" title="删除">
                                            <i class="bi bi-trash"></i>
                                        </a>
                                        <button class="btn btn-history btn-action" onclick="showHistory(<?= $p['id'] ?>)" title="查看历史记录">
                                            <i class="bi bi-clock-history"></i>
                                        </button>
                                    </div>
                                </td>
                            </tr>
                        <?php endforeach; ?>
                    </tbody>
                </table>
            </div>
            <div style="height: 80px;"></div>
        </div>
    <?php endif; ?>

    <!-- 详细模态框 -->
    <div class="modal fade detail-modal" id="detailModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header bg-info text-white">
                    <h5 class="modal-title"><i class="bi bi-eye"></i> 密码详情</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div id="detailContent"></div>
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">
                        <i class="bi bi-x"></i> 关闭
                    </button>
                    <button type="button" class="btn btn-warning" onclick="editCurrentRecord()">
                        <i class="bi bi-pencil"></i> 编辑此记录
                    </button>
                </div>
            </div>
        </div>
    </div>

    <!-- 历史记录模态框 -->
    <div class="modal fade" id="historyModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content">
                <div class="modal-header bg-secondary">
                    <h5 class="modal-title text-white"><i class="bi bi-clock-history"></i> 密码历史记录</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="table-responsive">
                        <table class="table table-bordered table-sm">
                            <thead class="table-light">
                                <tr>
                                    <th style="width: 35%;">旧密码</th>
                                    <th style="width: 35%;">新密码</th>
                                    <th style="width: 30%;">修改时间</th>
                                </tr>
                            </thead>
                            <tbody id="historyBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <div class="toast-container">
        <div class="toast" id="errorToast" role="alert">
            <div class="toast-header bg-warning text-dark">
                <i class="bi bi-exclamation-triangle me-2"></i>
                <strong class="me-auto">提示</strong>
                <button type="button" class="btn-close" data-bs-dismiss="toast"></button>
            </div>
            <div class="toast-body" id="toastBody"></div>
        </div>
    </div>
</div>

<script src="/style/bootstrap/5.3.2/js/bootstrap.bundle.min.js"></script>
<script>
    // 你原来的全部 JS 完全保留，只改了中文提示
    let currentRecord = null;
    function clearSearchAndReturn() {
        document.getElementById('searchInput').value = '';
        window.location.href = 'manage92.php';
    }
    document.addEventListener('DOMContentLoaded', function () {
        const formState = localStorage.getItem('formState');
        const formContainer = document.getElementById('formContainer');
        formContainer.style.display = formState === 'expanded' ? 'block' : 'none';
        document.getElementById('toggleFormBtn').addEventListener('click', function () {
            formContainer.style.display = formContainer.style.display === 'none' ? 'block' : 'none';
            localStorage.setItem('formState', formContainer.style.display === 'block' ? 'expanded' : 'collapsed');
        });
        const searchInput = document.getElementById('searchInput');
        const clearSearchBtn = document.getElementById('clearSearch');
        const searchForm = document.querySelector('.search-form');
        function toggleClearButton() {
            clearSearchBtn.style.display = searchInput.value.trim() ? 'block' : 'none';
        }
        toggleClearButton();
        searchInput.addEventListener('input', toggleClearButton);
        clearSearchBtn.addEventListener('click', () => {
            searchInput.value = '';
            toggleClearButton();
            searchForm.submit();
        });
        searchInput.addEventListener('keypress', e => {
            if (e.key === 'Enter') searchForm.submit();
        });
    });

    function showDetail(data) {
        currentRecord = data;
        const detailContent = document.getElementById('detailContent');
        const urlSection = data.url ? `
            <div class="detail-section detail-info">
                <h6><i class="bi bi-globe text-info"></i> 网站 URL</h6>
                <p><a href="${data.url}" target="_blank" class="detail-url"><i class="bi bi-box-arrow-up-right"></i> ${data.url}</a></p>
            </div>
        ` : `<div class="detail-section detail-info"><h6><i class="bi bi-globe text-muted"></i> 网站 URL</h6><p class="text-muted">未设置</p></div>`;
        const noteSection = data.note ? `
            <div class="detail-section detail-info">
                <h6><i class="bi bi-card-text text-secondary"></i> 备注</h6>
                <p class="text-break">${data.note}</p>
            </div>
        ` : `<div class="detail-section detail-info"><h6><i class="bi bi-card-text text-muted"></i> 备注</h6><p class="text-muted">无备注</p></div>`;
        detailContent.innerHTML = `
            <div class="row">
                <div class="col-md-6">
                    <div class="detail-section detail-info">
                        <h6><i class="bi bi-building text-primary"></i> 站点名称</h6>
                        <p class="fw-bold text-primary fs-5">${data.site_name}</p>
                    </div>
                    <div class="detail-section detail-info">
                        <h6><i class="bi bi-person text-success"></i> 用户名</h6>
                        <p class="fs-6">${data.username || '未设置'}</p>
                    </div>
                    <div class="detail-section detail-info">
                        <h6><i class="bi bi-lock text-danger"></i> 密码</h6>
                        <p class="fs-6 fw-semibold text-danger">${data.password || '未设置'}</p>
                    </div>
                </div>
                <div class="col-md-6">
                    <div class="detail-section detail-info">
                        <h6><i class="bi bi-calendar-check text-info"></i> 创建时间</h6>
                        <p>${data.created_at || '未知'}</p>
                    </div>
                    <div class="detail-section detail-info">
                        <h6><i class="bi bi-calendar3 text-secondary"></i> 更新时间</h6>
                        <p>${data.updated_at || '未知'}</p>
                    </div>
                </div>
            </div>
            ${urlSection}
            ${noteSection}
        `;
        new bootstrap.Modal('#detailModal').show();
    }

    function editCurrentRecord() {
        if (currentRecord) {
            edit(currentRecord);
            bootstrap.Modal.getInstance(document.getElementById('detailModal')).hide();
        }
    }

    function edit(data) {
        document.getElementById('id').value = data.id;
        document.getElementById('site_name').value = data.site_name;
        document.getElementById('url').value = data.url || '';
        document.getElementById('username').value = data.username || '';
        document.getElementById('password').value = data.password || '';
        document.getElementById('note').value = data.note || '';
        document.getElementById('formContainer').style.display = 'block';
        localStorage.setItem('formState', 'expanded');
        document.getElementById('formContainer').scrollIntoView({behavior: 'smooth'});
    }

    function clearForm() {
        document.getElementById('passwordForm').reset();
        document.getElementById('id').value = '';
    }

    async function showHistory(id) {
        try {
            const response = await fetch(`get_history.php?id=${id}`);
            const data = await response.json();
            const tbody = document.getElementById('historyBody');
            tbody.innerHTML = data.length === 0
                ? '<tr><td colspan="3" class="text-center text-muted py-4">暂无历史记录</td></tr>'
                : data.map(item => `
                    <tr>
                        <td class="text-break small">${item.old_password}</td>
                        <td class="text-break small">${item.new_password}</td>
                        <td class="small">${item.updated_at}</td>
                    </tr>
                `).join('');
            new bootstrap.Modal('#historyModal').show();
        } catch (error) {
            console.error('Error:', error);
            showToast('获取历史记录失败', 'warning');
        }
    }

    function showToast(message, type = 'warning') {
        const toastElement = document.getElementById('errorToast');
        const toastBody = document.getElementById('toastBody');
        const header = toastElement.querySelector('.toast-header');
        header.className = type === 'success' ? 'toast-header bg-success text-white' : 'toast-header bg-warning text-dark';
        toastBody.textContent = message;
        new bootstrap.Toast(toastElement).show();
    }

    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
            e.preventDefault();
            clearForm();
            document.getElementById('formContainer').style.display = 'block';
            localStorage.setItem('formState', 'expanded');
            document.getElementById('site_name').focus();
        }
        if (e.key === 'Escape') {
            document.querySelectorAll('.modal.show').forEach(modal => {
                bootstrap.Modal.getInstance(modal)?.hide();
            });
            const searchInput = document.getElementById('searchInput');
            if (searchInput && searchInput.value) {
                searchInput.value = '';
                document.querySelector('.search-form').submit();
            }
        }
    });

    document.getElementById('passwordForm').addEventListener('submit', function(e) {
        if (!document.getElementById('site_name').value.trim()) {
            e.preventDefault();
            showToast('站点名称不能为空！', 'error');
            document.getElementById('site_name').focus();
        }
    });
</script>
</body>
</html>
