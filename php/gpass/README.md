### 数据库初始化
```mysql
-- 创建 passwords 表，存储密码信息
CREATE TABLE passwords (
    id INT AUTO_INCREMENT PRIMARY KEY,     -- 唯一标识符，自增长
    site_name VARCHAR(255) NOT NULL,       -- 站点名称
    url VARCHAR(255),                      -- 网站 URL
    username VARCHAR(255),                 -- 用户名
    password VARCHAR(255) NOT NULL,        -- 密码
    note TEXT,                             -- 备注
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 创建时间，默认为当前时间
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP -- 更新时间
);

-- 创建 password_history 表，存储密码历史记录
CREATE TABLE password_history (
    id INT AUTO_INCREMENT PRIMARY KEY,     -- 唯一标识符，自增长
    password_id INT,                       -- 密码表的 id，外键
    old_password VARCHAR(255),             -- 旧密码
    new_password VARCHAR(255),             -- 新密码
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 修改时间
    FOREIGN KEY (password_id) REFERENCES passwords(id) ON DELETE CASCADE
);

-- 创建 search_history 表，存储搜索历史记录
CREATE TABLE search_history (
    id INT AUTO_INCREMENT PRIMARY KEY,     -- 唯一标识符，自增长
    search_query VARCHAR(255) NOT NULL,     -- 搜索内容
    search_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP  -- 搜索时间
);

-- 添加索引以优化查询
CREATE INDEX idx_site_name ON passwords(site_name);
CREATE INDEX idx_url ON passwords(url);
CREATE INDEX idx_username ON passwords(username);
CREATE INDEX idx_search_query ON search_history(search_query);
```

### 示例数据初始化
```mysql
-- 创建 passwords 表，存储密码信息
CREATE TABLE passwords (
    id INT AUTO_INCREMENT PRIMARY KEY,     -- 唯一标识符，自增长
    site_name VARCHAR(255) NOT NULL,       -- 站点名称
    url VARCHAR(255),                      -- 网站 URL
    username VARCHAR(255),                 -- 用户名
    password VARCHAR(255) NOT NULL,        -- 密码
    note TEXT,                             -- 备注
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 创建时间，默认为当前时间
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP -- 更新时间
);

-- 创建 password_history 表，存储密码历史记录
CREATE TABLE password_history (
    id INT AUTO_INCREMENT PRIMARY KEY,     -- 唯一标识符，自增长
    password_id INT,                       -- 密码表的 id，外键
    old_password VARCHAR(255),             -- 旧密码
    new_password VARCHAR(255),             -- 新密码
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- 修改时间
    FOREIGN KEY (password_id) REFERENCES passwords(id) ON DELETE CASCADE
);

-- 创建 search_history 表，存储搜索历史记录
CREATE TABLE search_history (
    id INT AUTO_INCREMENT PRIMARY KEY,     -- 唯一标识符，自增长
    search_query VARCHAR(255) NOT NULL,     -- 搜索内容
    search_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP  -- 搜索时间
);

-- 添加索引以优化查询
CREATE INDEX idx_site_name ON passwords(site_name);
CREATE INDEX idx_url ON passwords(url);
CREATE INDEX idx_username ON passwords(username);
CREATE INDEX idx_search_query ON search_history(search_query);

-- 插入示例数据到 passwords 表
INSERT INTO passwords (site_name, url, username, password, note)
VALUES
    ('Example Site 1', 'https://example1.com', 'user1', 'password1', 'This is a note for example 1'),
    ('Example Site 2', 'https://example2.com', 'user2', 'password2', 'This is a note for example 2');

-- 插入示例数据到 password_history 表
-- 假设 passwords 表中的 ID 是 1 和 2
INSERT INTO password_history (password_id, old_password, new_password)
VALUES
    (1, 'old_password1', 'new_password1'),
    (2, 'old_password2', 'new_password2');

-- 插入示例数据到 search_history 表
INSERT INTO search_history (search_query)
VALUES
    ('example search 1'),
    ('example search 2');
```