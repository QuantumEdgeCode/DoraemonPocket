<?php
require 'db.php';

if (isset($_GET['id'])) {
    $id = intval($_GET['id']);
    $stmt = $pdo->prepare("SELECT * FROM password_history WHERE password_id = ? ORDER BY updated_at DESC");
    $stmt->execute([$id]);
    $history = $stmt->fetchAll();
    
    header('Content-Type: application/json');
    echo json_encode($history);
    exit;
}

http_response_code(400);
echo json_encode(['error' => 'Invalid request']);