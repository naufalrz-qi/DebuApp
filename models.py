from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

DEBT_CATEGORIES    = ['Kendaraan', 'Elektronik', 'KPR', 'Pinjaman Pribadi', 'Kartu Kredit', 'Lainnya']
INCOME_CATEGORIES  = ['Gaji', 'Bonus', 'Freelance', 'Investasi / Dividen', 'Penjualan', 'Lainnya']
EXPENSE_CATEGORIES = ['Makan & Minum', 'Transportasi', 'Tagihan & Utilitas', 'Kesehatan',
                      'Pendidikan', 'Hiburan & Lifestyle', 'Belanja', 'Kebutuhan Rumah', 'Lainnya']
ASSET_CATEGORIES   = ['Tabungan / Deposito', 'Investasi (Saham/Reksa Dana)', 'Kendaraan',
                      'Properti / Tanah', 'Emas / Logam Mulia', 'Lainnya']
MONTH_NAMES = ['Januari','Februari','Maret','April','Mei','Juni',
               'Juli','Agustus','September','Oktober','November','Desember']

class Debt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=True, default='Lainnya')
    total_amount = db.Column(db.Float, nullable=False)
    remaining_amount = db.Column(db.Float, nullable=False, default=0.0)
    tenure_months = db.Column(db.Integer, nullable=False)
    monthly_installment = db.Column(db.Float, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    paid_until = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    __tablename__ = 'cash_transaction'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(10), nullable=False)       # income | expense | transfer
    category = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(200), nullable=True)
    payment_method = db.Column(db.String(10), nullable=False, default='cash')
    wallet_name = db.Column(db.String(50), nullable=True)
    transfer_to_wallet = db.Column(db.String(50), nullable=True)  # untuk type='transfer'
    is_recurring = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class WalletAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    method = db.Column(db.String(10), nullable=False)   # cash | digital
    notes = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SavingsGoal(db.Model):
    """Target tabungan dengan progress tracking."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    target_amount = db.Column(db.Float, nullable=False)
    current_amount = db.Column(db.Float, nullable=False, default=0.0)
    target_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Asset(db.Model):
    """Aset untuk kalkulasi Net Worth."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False, default='Lainnya')
    value = db.Column(db.Float, nullable=False)
    notes = db.Column(db.String(200), nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SpendingLimit(db.Model):
    """Batas pengeluaran per kategori per bulan."""
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(100), nullable=False)
    limit_amount = db.Column(db.Float, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('category', 'month', 'year', name='uq_limit_cat_month_year'),)
