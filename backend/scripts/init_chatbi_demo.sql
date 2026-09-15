-- ═══════════════════════════════════════════════════════════════
-- chatbi 演示业务库初始化脚本(独立演示库形态)
--
-- 用途:为 ChatBI 插件准备一个独立的演示数据源库(推荐库名 chatbi_biz_demo)。
-- 之前演示表(biz_*)混在运行库里,被 pytest 清库连坐清掉(两次事故);
-- 独立成库后,运行库随便清/重建,演示数据源毫发无损。
--
-- 用法(在 chatbi-postgres 容器里,或任何 PG 16 实例):
--   1. 建库(只需一次):
--      docker exec chatbi-postgres psql -U root -d postgres \
--        -c "CREATE DATABASE chatbi_biz_demo OWNER root;"
--   2. 灌表+数据:
--      docker exec -i chatbi-postgres psql -U root -d chatbi_biz_demo \
--        < backend/scripts/init_chatbi_demo.sql
--   3. 在「智能问数 → 数据源」页新建数据源:
--      名称=演示业务库  类型=PostgreSQL  主机=localhost  端口=5432
--      数据库=chatbi_biz_demo  用户=root  密码=root
--   4. 点「扫描」→ 语义层/图谱/对话全链路即可用。
--
-- 幂等:表结构 CREATE IF NOT EXISTS;数据部分 TRUNCATE 后重灌,
-- 可重复执行(结果可复现,random 固定种子语义由固定 INSERT 实现)。
-- ═══════════════════════════════════════════════════════════════

-- ── DDL(含注释——扫描期"📋 注释"来源徽标的根基,不能省) ──

CREATE TABLE IF NOT EXISTS biz_users (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(128) NOT NULL,
    email VARCHAR(255),
    phone VARCHAR(32),
    city VARCHAR(64),
    vip_level SMALLINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);
COMMENT ON TABLE biz_users IS '用户表';
COMMENT ON COLUMN biz_users.username IS '用户名';
COMMENT ON COLUMN biz_users.email IS '邮箱';
COMMENT ON COLUMN biz_users.phone IS '手机号';
COMMENT ON COLUMN biz_users.city IS '所在城市';
COMMENT ON COLUMN biz_users.vip_level IS 'VIP等级(0-5)';
COMMENT ON COLUMN biz_users.created_at IS '注册时间';

CREATE TABLE IF NOT EXISTS biz_categories (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    parent_id BIGINT REFERENCES biz_categories(id),
    sort_order INT DEFAULT 0
);
COMMENT ON TABLE biz_categories IS '商品分类表';
COMMENT ON COLUMN biz_categories.name IS '分类名称';
COMMENT ON COLUMN biz_categories.parent_id IS '父分类ID';
COMMENT ON COLUMN biz_categories.sort_order IS '排序权重';

CREATE TABLE IF NOT EXISTS biz_products (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(256) NOT NULL,
    category_id BIGINT REFERENCES biz_categories(id),
    price DECIMAL(10,2) NOT NULL,
    stock INT DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);
COMMENT ON TABLE biz_products IS '商品表';
COMMENT ON COLUMN biz_products.name IS '商品名称';
COMMENT ON COLUMN biz_products.category_id IS '所属分类';
COMMENT ON COLUMN biz_products.price IS '售价';
COMMENT ON COLUMN biz_products.stock IS '库存数量';
COMMENT ON COLUMN biz_products.is_active IS '是否上架';
COMMENT ON COLUMN biz_products.created_at IS '上架时间';

CREATE TABLE IF NOT EXISTS biz_orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES biz_users(id),
    order_no VARCHAR(64) NOT NULL UNIQUE,
    total_amount DECIMAL(12,2) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    payment_method VARCHAR(32),
    remark TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    paid_at TIMESTAMP,
    shipped_at TIMESTAMP
);
COMMENT ON TABLE biz_orders IS '订单表';
COMMENT ON COLUMN biz_orders.user_id IS '下单用户ID';
COMMENT ON COLUMN biz_orders.order_no IS '订单编号';
COMMENT ON COLUMN biz_orders.total_amount IS '订单总金额(含税)';
COMMENT ON COLUMN biz_orders.status IS '订单状态(pending/paid/shipped/cancelled)';
COMMENT ON COLUMN biz_orders.payment_method IS '支付方式';
COMMENT ON COLUMN biz_orders.created_at IS '下单时间';
COMMENT ON COLUMN biz_orders.paid_at IS '支付时间';
COMMENT ON COLUMN biz_orders.shipped_at IS '发货时间';

CREATE TABLE IF NOT EXISTS biz_order_items (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES biz_orders(id),
    product_id BIGINT NOT NULL REFERENCES biz_products(id),
    quantity INT NOT NULL,
    unit_price DECIMAL(10,2) NOT NULL,
    subtotal DECIMAL(12,2) GENERATED ALWAYS AS (quantity * unit_price) STORED
);
COMMENT ON TABLE biz_order_items IS '订单明细表';
COMMENT ON COLUMN biz_order_items.order_id IS '所属订单ID';
COMMENT ON COLUMN biz_order_items.product_id IS '商品ID';
COMMENT ON COLUMN biz_order_items.quantity IS '购买数量';
COMMENT ON COLUMN biz_order_items.unit_price IS '成交单价';
COMMENT ON COLUMN biz_order_items.subtotal IS '小计金额';

-- ── 样本数据(可复现) ────────────────────────────────────────

TRUNCATE biz_order_items, biz_orders, biz_products, biz_categories, biz_users
    RESTART IDENTITY CASCADE;

-- 分类(7): 3 个一级 + 4 个二级
INSERT INTO biz_categories (name, parent_id, sort_order) VALUES
    ('数码', NULL, 3), ('服装', NULL, 7), ('食品', NULL, 5),
    ('手机', 1, 2), ('笔记本', 1, 9), ('男装', 2, 4), ('女装', 2, 6);

-- 用户(50): 6 城分布, VIP 0-5
INSERT INTO biz_users (username, email, phone, city, vip_level, created_at)
SELECT
    'user_' || lpad(g::text, 3, '0'),
    'user' || g || '@example.com',
    '138' || lpad(((g * 7919) % 90000000 + 10000000)::text, 8, '0'),
    (ARRAY['北京','上海','广州','深圳','杭州','成都'])[(g % 6) + 1],
    g % 6,
    NOW() - ((g * 13) % 365 || ' days')::interval
FROM generate_series(1, 50) g;

-- 商品(30): 均匀挂到 7 个分类, 价格 9.9-999.9
INSERT INTO biz_products (name, category_id, price, stock, is_active, created_at)
SELECT
    '商品_' || lpad(g::text, 3, '0'),
    (g % 7) + 1,
    round((9.9 + ((g * 37) % 990) * 1.0)::numeric, 2),
    (g * 31) % 1000,
    (g % 5) <> 0,
    NOW() - ((g * 7) % 200 || ' days')::interval
FROM generate_series(1, 30) g;

-- 订单(200): 状态按 2:5:4:1 权重(pending/paid/shipped/cancelled),
-- 金额 50-3050, 支付方式轮转, 时间散布近 90 天
INSERT INTO biz_orders (user_id, order_no, total_amount, status,
                        payment_method, created_at, paid_at, shipped_at)
SELECT
    (g % 50) + 1,
    'ORD' || (20260000 + g),
    round((50 + (g * 53) % 3000)::numeric, 2),
    (ARRAY['pending','paid','shipped','cancelled'])[
        CASE (g % 12)
            WHEN 0 THEN 1 WHEN 1 THEN 1
            WHEN 2 THEN 2 WHEN 3 THEN 2 WHEN 4 THEN 2 WHEN 5 THEN 2 WHEN 6 THEN 2
            WHEN 7 THEN 3 WHEN 8 THEN 3 WHEN 9 THEN 3 WHEN 10 THEN 3
            ELSE 4 END],
    (ARRAY['alipay','wechat','card',NULL])[(g % 4) + 1],
    NOW() - ((g * 3) % 90 || ' days')::interval,
    CASE WHEN (g % 12) IN (2,3,4,5,6) OR (g % 12) IN (7,8,9,10)
         THEN NOW() - ((g * 3) % 90 || ' days')::interval + ((g % 48) || ' hours')::interval
         ELSE NULL END,
    CASE WHEN (g % 12) IN (7,8,9,10)
         THEN NOW() - ((g * 3) % 90 || ' days')::interval + (((g % 48) + 48) || ' hours')::interval
         ELSE NULL END
FROM generate_series(1, 200) g;

-- 订单明细(每单 2-5 行, 单价取商品价): 用确定性伪随机展开
INSERT INTO biz_order_items (order_id, product_id, quantity, unit_price)
SELECT
    o.id,
    ((o.id * 7 + n * 11) % 30) + 1,
    ((o.id + n) % 3) + 1,
    p.price
FROM biz_orders o
CROSS JOIN LATERAL (
    SELECT n FROM generate_series(1, 2 + (o.id % 4)) n
) lines
JOIN biz_products p ON p.id = ((o.id * 7 + n * 11) % 30) + 1;

-- ── 验证 ─────────────────────────────────────────────────────
-- 预期: users=50, categories=7, products=30, orders=200, items≈700
SELECT 'biz_users' AS t, count(*) FROM biz_users
UNION ALL SELECT 'biz_categories', count(*) FROM biz_categories
UNION ALL SELECT 'biz_products', count(*) FROM biz_products
UNION ALL SELECT 'biz_orders', count(*) FROM biz_orders
UNION ALL SELECT 'biz_order_items', count(*) FROM biz_order_items;
