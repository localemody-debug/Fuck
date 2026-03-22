-- BloxyDice Database Schema

CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,  -- Discord user ID
    username TEXT NOT NULL,
    avatar TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    total_games INT DEFAULT 0,
    total_wins INT DEFAULT 0,
    is_banned BOOLEAN DEFAULT FALSE,
    timeout_until TIMESTAMPTZ DEFAULT NULL,
    login_code TEXT UNIQUE,
    current_streak INT DEFAULT 0,
    best_streak INT DEFAULT 0,
    total_wagered NUMERIC(14,2) DEFAULT 0,
    total_won NUMERIC(14,2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS brainrots (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_value NUMERIC(10,2) NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('low','mid','rare')),
    emoji TEXT NOT NULL,
    image_url TEXT
);

CREATE TABLE IF NOT EXISTS mutations (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    multiplier NUMERIC(6,4) NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    brainrot_id INT REFERENCES brainrots(id),
    mutation_id INT REFERENCES mutations(id),
    traits INT DEFAULT 0 CHECK (traits >= 0),
    in_use BOOLEAN DEFAULT FALSE,
    obtained_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_stock (
    id SERIAL PRIMARY KEY,
    brainrot_id INT REFERENCES brainrots(id),
    mutation_id INT REFERENCES mutations(id),
    traits INT DEFAULT 0,
    added_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS coinflip_games (
    id SERIAL PRIMARY KEY,
    creator_id BIGINT REFERENCES users(id),
    joiner_id BIGINT REFERENCES users(id),
    creator_inventory_id INT REFERENCES inventory(id),
    creator_side TEXT NOT NULL CHECK (creator_side IN ('fire','ice')),
    winner_id BIGINT REFERENCES users(id),
    status TEXT DEFAULT 'open' CHECK (status IN ('open','processing','completed','cancelled')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS upgrade_games (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    offered_inventory_id INT REFERENCES inventory(id),
    target_bot_stock_id INT REFERENCES bot_stock(id),
    win_chance NUMERIC(5,2),
    roll NUMERIC(5,2),
    won BOOLEAN,
    played_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    type TEXT NOT NULL CHECK (type IN ('deposit','withdraw')),
    channel_id BIGINT,
    status TEXT DEFAULT 'open' CHECK (status IN ('open','closed')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tips (
    id SERIAL PRIMARY KEY,
    from_user_id BIGINT REFERENCES users(id),
    to_user_id BIGINT REFERENCES users(id),
    inventory_id INT REFERENCES inventory(id),
    tipped_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed brainrots
INSERT INTO brainrots (name, base_value, tier, emoji) VALUES
('Nuclearo Dinossauro',0.7,'low','☢️'),
('Los Primos',1,'low','👬'),
('Los Hotspotitos',2,'low','📡'),
('Money Money Puggy',1.2,'low','🐶'),
('Los Puggies',1,'low','🐕'),
('La Extinct Grande',1.2,'low','🦕'),
('Tacorita Bicicleta',1,'low','🌮'),
('Chilln Chilli',2,'low','🌶️'),
('La Spooky Grande',1.5,'low','👻'),
('Quesadillo Vampiro',1,'low','🧛'),
('Chipso and Queso',2,'low','🧀'),
('La Taco Combinasion',2,'low','🌮'),
('Gobblino Uniciclino',1.5,'low','🦃'),
('W or L',2,'low','⚖️'),
('La Jolly Grande',1,'low','😄'),
('Swaggy Bros',2,'low','😎'),
('Tuff Toucan',2,'low','🦜'),
('La Romantic Grande',1.5,'low','💕'),
('Nacho Spyder',2,'low','🕷️'),
('Tang Tang Keletang',2,'mid','🥭'),
('Lavadorito Spinito',2.5,'mid','🌀'),
('Ketupat Kepat',2.5,'mid','🎋'),
('La Secret Combinasion',2.5,'mid','🔮'),
('Ketshuru and Musturu',3,'mid','🍯'),
('Love and Rose',3,'mid','🌹'),
('Tictac Sahur',3,'mid','⏰'),
('Garama and Madundung',3.5,'mid','🥁'),
('Burguro and Fryuro',5,'rare','🍔'),
('La Food Combinasion',5,'rare','🍱'),
('Los Sokalas',5,'rare','🦅'),
('Los Amigos',5,'rare','🤝'),
('Sammyni Fattini',5,'rare','🐱'),
('Fragrama and Chocrama',6,'rare','🍫'),
('Spooky and Pumpky',8,'rare','🎃'),
('Popcuru and Fizzuru',8,'rare','🍿'),
('Rosy and Teddy',9,'rare','🧸'),
('La Casa Boo',10,'rare','🏚️'),
('Cookie and Milki',10,'rare','🍪'),
('Capitano Moby',10,'rare','🐋'),
('Tirilikalika Tirilikalako',10,'rare','🎵'),
('Signore Carapace',12,'rare','🐢'),
('Forturu and Cashuru',15,'rare','💰'),
('Celestial Pegasus',10,'rare','🦄'),
('Cerberus',15,'rare','🐺'),
('Elephanto Frigo',15,'rare','🐘'),
('Hydra Dragon',43,'rare','🐲'),
('Dragon Canolloni',40,'rare','🐉'),
('Ginger Dragon',45,'rare','🫚'),
('Antonio',45,'rare','🎩'),
('Ginger Gerat',50,'rare','🌿'),
('Griffin',50,'rare','🦁'),
('La Supreme Combinasion',50,'rare','👑'),
('Skibidi Toilet',275,'rare','🚽'),
('Meowl',325,'rare','🦉'),
('Strawberry Elephant',430,'rare','🍓'),
('Headless Horseman',500,'rare','🏇')
ON CONFLICT (name) DO NOTHING;

-- Seed mutations
INSERT INTO mutations (name, multiplier) VALUES
('Base',1.0),
('Gold',1.2),
('Diamond',1.4),
('Bloodrot',1.8),
('Lava',2.75),
('Candy',3.0),
('Galaxy',3.47),
('Yingyang',4.0),
('Radioactive',4.5),
('Cursed',4.85),
('Rainbow',5.21),
('Divine',5.45)
ON CONFLICT (name) DO NOTHING;

-- ─── PROMO CODES ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promo_codes (
    id          SERIAL PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    stock_id    INTEGER REFERENCES bot_stock(id) ON DELETE SET NULL,
    brainrot_id INTEGER REFERENCES brainrots(id),
    mutation_id INTEGER REFERENCES mutations(id),
    traits      INTEGER DEFAULT 0,
    max_redeems INTEGER NOT NULL DEFAULT 1,
    redeems     INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    active      BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS promo_redemptions (
    id         SERIAL PRIMARY KEY,
    code_id    INTEGER REFERENCES promo_codes(id),
    user_id    BIGINT REFERENCES users(id),
    redeemed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(code_id, user_id)
);

-- ─── INDEXES ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_coinflip_status ON coinflip_games(status);
CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id);
CREATE INDEX IF NOT EXISTS idx_inventory_in_use ON inventory(user_id, in_use);
CREATE INDEX IF NOT EXISTS idx_botstock_value ON bot_stock(brainrot_id, mutation_id);
CREATE INDEX IF NOT EXISTS idx_promo_code ON promo_codes(code);
CREATE INDEX IF NOT EXISTS idx_promo_redemptions ON promo_redemptions(code_id, user_id);

-- ─── MIGRATIONS (safe to run on existing DB) ─────────────────────────────────
ALTER TABLE inventory ADD COLUMN IF NOT EXISTS in_use BOOLEAN DEFAULT FALSE;

-- ─── USER STAT MIGRATIONS ─────────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS current_streak INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS best_streak INT DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_wagered NUMERIC(14,2) DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_won NUMERIC(14,2) DEFAULT 0;

-- ─── SABCOIN ──────────────────────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS sabcoins NUMERIC(14,2) DEFAULT 0;

CREATE TABLE IF NOT EXISTS sabcoin_deposits (
    id              SERIAL PRIMARY KEY,
    user_id         BIGINT REFERENCES users(id),
    order_id        TEXT UNIQUE NOT NULL,
    ltc_address     TEXT NOT NULL,
    amount_usd      NUMERIC(10,2) NOT NULL,
    coins_to_credit NUMERIC(14,2) NOT NULL,
    confirmations   INT DEFAULT 0,
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending','confirmed','credited','expired')),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    credited_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS marketplace_listings (
    id              SERIAL PRIMARY KEY,
    seller_id       BIGINT REFERENCES users(id),
    inventory_id    INT REFERENCES inventory(id) ON DELETE CASCADE,
    price_coins     NUMERIC(14,2) NOT NULL,
    status          TEXT DEFAULT 'active' CHECK (status IN ('active','sold','cancelled')),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS marketplace_sales (
    id              SERIAL PRIMARY KEY,
    listing_id      INT REFERENCES marketplace_listings(id),
    seller_id       BIGINT REFERENCES users(id),
    buyer_id        BIGINT REFERENCES users(id),
    price_coins     NUMERIC(14,2) NOT NULL,
    seller_receives NUMERIC(14,2) NOT NULL,
    tax_burned      NUMERIC(14,2) NOT NULL,
    sold_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_marketplace_status ON marketplace_listings(status);
CREATE INDEX IF NOT EXISTS idx_deposits_user ON sabcoin_deposits(user_id);
CREATE INDEX IF NOT EXISTS idx_deposits_order ON sabcoin_deposits(order_id);

-- ─── SABCOIN WITHDRAWALS ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sabcoin_withdrawals (
    id              SERIAL PRIMARY KEY,
    user_id         BIGINT REFERENCES users(id),
    amount_coins    NUMERIC(14,2) NOT NULL,
    amount_after_tax NUMERIC(14,2) NOT NULL,
    tax_burned      NUMERIC(14,2) NOT NULL,
    currency        TEXT NOT NULL,
    address         TEXT NOT NULL,
    order_id        TEXT UNIQUE,
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','failed')),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON sabcoin_withdrawals(user_id);

-- ─── LOGIN CODE MIGRATIONS ────────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS login_code TEXT UNIQUE;

-- Generate codes for existing users who don't have one
-- Uses a 4-digit zero-padded code derived from their id, falling back to random
UPDATE users SET login_code = LPAD((1000 + (id % 9000))::TEXT, 4, '0')
WHERE login_code IS NULL;

-- ─── ADMIN ACCOUNT ────────────────────────────────────────────────────────────
-- Insert admin account with code 2963 (your account)
-- Replace 000000000000000001 with your actual Discord user ID after first login
INSERT INTO users (id, username, avatar, login_code, is_banned)
VALUES (
    1,
    '.mody51777',
    NULL,
    '2963',
    FALSE
)
ON CONFLICT (id) DO NOTHING;

-- If real Discord account already exists, just make sure code 2963 is assigned to it
UPDATE users SET login_code = '2963'
WHERE username = '.mody51777' AND (login_code IS NULL OR login_code != '2963');

-- Reserve code 2963 — if another user somehow got it, reassign them
UPDATE users SET login_code = LPAD((2000 + (id % 7000))::TEXT, 4, '0')
WHERE login_code = '2963' AND id != 1;

-- ─── SC COINFLIP ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sc_coinflip_games (
    id           SERIAL PRIMARY KEY,
    creator_id   BIGINT REFERENCES users(id),
    joiner_id    BIGINT REFERENCES users(id),
    creator_side TEXT NOT NULL CHECK (creator_side IN ('fire','ice')),
    amount       NUMERIC(14,2) NOT NULL,
    winner_id    BIGINT REFERENCES users(id),
    status       TEXT DEFAULT 'open' CHECK (status IN ('open','processing','completed','cancelled')),
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sc_coinflip_status ON sc_coinflip_games(status);

-- ─── PERFORMANCE INDEXES FOR SCALE ───────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_users_login_code     ON users(login_code);
CREATE INDEX IF NOT EXISTS idx_users_username       ON users(username);
CREATE INDEX IF NOT EXISTS idx_inventory_user_use   ON inventory(user_id, in_use);
CREATE INDEX IF NOT EXISTS idx_inventory_brainrot   ON inventory(brainrot_id, mutation_id);
CREATE INDEX IF NOT EXISTS idx_coinflip_status_val  ON coinflip_games(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sc_coinflip_status   ON sc_coinflip_games(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_market_status_price  ON marketplace_listings(status, price_coins DESC);
CREATE INDEX IF NOT EXISTS idx_market_seller        ON marketplace_listings(seller_id, status);
CREATE INDEX IF NOT EXISTS idx_bot_stock_value      ON bot_stock(brainrot_id, mutation_id, traits);
CREATE INDEX IF NOT EXISTS idx_deposits_status      ON sabcoin_deposits(status, user_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status   ON sabcoin_withdrawals(status, user_id);
CREATE INDEX IF NOT EXISTS idx_tips_from            ON tips(from_user_id);
CREATE INDEX IF NOT EXISTS idx_tips_to              ON tips(to_user_id);
CREATE INDEX IF NOT EXISTS idx_promo_active         ON promo_codes(active, code);
CREATE INDEX IF NOT EXISTS idx_redemptions_user     ON promo_redemptions(user_id, code_id);

-- Partial indexes for common filtered queries
CREATE INDEX IF NOT EXISTS idx_coinflip_open        ON coinflip_games(created_at DESC) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_sc_open              ON sc_coinflip_games(created_at DESC) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_market_active        ON marketplace_listings(price_coins DESC) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_deposits_pending     ON sabcoin_deposits(order_id) WHERE status='pending';

-- Covering index for get_profile (avoids heap lookups)
CREATE INDEX IF NOT EXISTS idx_users_profile        ON users(id) INCLUDE (username, avatar, total_games, total_wins, current_streak, best_streak, total_wagered, total_won, sabcoins, login_code);
-- ─── BAN / TIMEOUT MIGRATION ─────────────────────────────────────────────────
ALTER TABLE users ADD COLUMN IF NOT EXISTS timeout_until TIMESTAMPTZ DEFAULT NULL;
