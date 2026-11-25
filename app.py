import os, math, json, csv
from io import StringIO
from flask import Flask, render_template, request, redirect, url_for, flash, Response
from models import (db, Debt, Transaction, WalletAccount,
                    SavingsGoal, Asset, SpendingLimit,
                    DEBT_CATEGORIES, INCOME_CATEGORIES, EXPENSE_CATEGORIES,
                    ASSET_CATEGORIES, MONTH_NAMES)
from datetime import datetime, date
import calendar

# ─── Helpers ───────────────────────────────────────────────

def add_months(sourcedate, months):
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    day = min(sourcedate.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

def enrich_debt(d, today=None):
    if today is None:
        today = date.today()
    if d.paid_until is None:
        d.paid_until = add_months(d.start_date, -1)
    d.is_paid_this_month = (
        d.paid_until.year > today.year or
        (d.paid_until.year == today.year and d.paid_until.month >= today.month)
    )
    d.not_started = (
        d.start_date.year > today.year or
        (d.start_date.year == today.year and d.start_date.month > today.month)
    )
    paid_amount = d.total_amount - d.remaining_amount
    d.progress_pct = round((paid_amount / d.total_amount) * 100, 1) if d.total_amount > 0 else 0
    d.months_remaining = (
        math.ceil(d.remaining_amount / d.monthly_installment)
        if d.monthly_installment > 0 and d.remaining_amount > 0 else 0
    )
    d.estimated_payoff = add_months(today, d.months_remaining) if d.months_remaining > 0 else None
    return d

def fmt_rp(val):
    return f"Rp {val:,.0f}"

def get_monthly_cashflow(month, year):
    txs = Transaction.query.filter_by(month=month, year=year).all()
    total_income  = sum(t.amount for t in txs if t.type == 'income')
    total_expense = sum(t.amount for t in txs if t.type == 'expense')
    return total_income, total_expense, txs

def get_wallet_balances(txs):
    cash_in  = sum(t.amount for t in txs if t.type == 'income'  and t.payment_method == 'cash')
    cash_out = sum(t.amount for t in txs if t.type == 'expense' and t.payment_method == 'cash')
    dig_in   = sum(t.amount for t in txs if t.type == 'income'  and t.payment_method == 'digital')
    dig_out  = sum(t.amount for t in txs if t.type == 'expense' and t.payment_method == 'digital')
    for t in txs:
        if t.type == 'transfer':
            if t.payment_method == 'cash': cash_out += t.amount
            else: dig_out += t.amount
            tw = WalletAccount.query.filter_by(name=t.transfer_to_wallet).first()
            if tw:
                if tw.method == 'cash': cash_in += t.amount
                else: dig_in += t.amount
    return {'cash_in': cash_in, 'cash_out': cash_out, 'cash_net': cash_in - cash_out,
            'dig_in': dig_in,   'dig_out': dig_out,   'dig_net': dig_in - dig_out}

def get_all_wallet_balances():
    all_txs = Transaction.query.all()
    wallets = {}
    for t in all_txs:
        src = t.wallet_name or ('Dompet Tunai' if t.payment_method == 'cash' else 'Lainnya')
        wallets.setdefault(src, 0)
        if t.type == 'income':   wallets[src] += t.amount
        elif t.type == 'expense': wallets[src] -= t.amount
        elif t.type == 'transfer':
            wallets[src] -= t.amount
            tgt = t.transfer_to_wallet or 'Lainnya'
            wallets.setdefault(tgt, 0)
            wallets[tgt] += t.amount
    return wallets

def get_trend_data(n=6):
    today = date.today()
    result = []
    for i in range(n-1, -1, -1):
        m, y = today.month - i, today.year
        while m <= 0: m += 12; y -= 1
        inc, exp, _ = get_monthly_cashflow(m, y)
        result.append({'label': MONTH_NAMES[m-1][:3]+"'"+str(y)[2:], 'income': inc, 'expense': exp})
    return result

def get_mom_comparison():
    today = date.today()
    cm, cy = today.month, today.year
    pm = cm - 1 if cm > 1 else 12
    py = cy if cm > 1 else cy - 1
    ci, ce, _ = get_monthly_cashflow(cm, cy)
    pi, pe, _ = get_monthly_cashflow(pm, py)
    def pct(a, b): return round((a-b)/b*100, 1) if b > 0 else 0
    return {'income_change': ci-pi, 'income_pct': pct(ci,pi),
            'expense_change': ce-pe, 'expense_pct': pct(ce,pe),
            'prev_month': MONTH_NAMES[pm-1]}

def get_spending_alerts(month, year):
    limits = SpendingLimit.query.filter_by(month=month, year=year).all()
    txs = Transaction.query.filter_by(month=month, year=year).all()
    alerts = []
    for lim in limits:
        spent = sum(t.amount for t in txs if t.type == 'expense' and t.category == lim.category)
        pct = round(spent / lim.limit_amount * 100, 1) if lim.limit_amount > 0 else 0
        if pct >= 80:
            alerts.append({'category': lim.category, 'spent': spent,
                           'limit': lim.limit_amount, 'pct': pct, 'over': pct >= 100})
    return alerts

//ini untuk aplikasi recurring 
def apply_recurring(target_month, target_year):
    pm = target_month - 1 if target_month > 1 else 12
    py = target_year if target_month > 1 else target_year - 1
    rec_txs = Transaction.query.filter_by(month=pm, year=py, is_recurring=True).all()
    copied = 0
    for tx in rec_txs:
        exists = Transaction.query.filter_by(month=target_month, year=target_year,
            category=tx.category, amount=tx.amount, is_recurring=True, type=tx.type).first()
        if not exists:
            day = min(tx.date.day, calendar.monthrange(target_year, target_month)[1])
            db.session.add(Transaction(
                date=date(target_year, target_month, day),
                month=target_month, year=target_year, type=tx.type,
                category=tx.category, amount=tx.amount, description=tx.description,
                payment_method=tx.payment_method, wallet_name=tx.wallet_name, is_recurring=True
            ))
            copied += 1
    db.session.commit()
    return copied

def generate_recommendations(actual_income, total_installment, actual_expense, debts, today):
    recs = []
    net_balance = actual_income - actual_expense - total_installment
    debt_ratio = (total_installment / actual_income * 100) if actual_income > 0 else 0
    savings_rate = (net_balance / actual_income * 100) if actual_income > 0 else 0
    active_debts = [d for d in debts if d.remaining_amount > 0 and not d.not_started]

    if actual_income == 0:
        recs.append({'type': 'warning', 'icon': '📋', 'title': 'Belum Ada Pemasukan Tercatat',
                     'body': 'Catat pemasukan di halaman Transaksi agar semua analisis keuangan bisa dihitung akurat.'})
        return recs

    # 1. Net Cash Flow
    if net_balance < 0:
        recs.append({'type': 'danger', 'icon': '🚨', 'title': 'Arus Kas Negatif Bulan Ini!',
                     'body': f'Total keluar ({fmt_rp(actual_expense + total_installment)}) > pemasukan ({fmt_rp(actual_income)}). '
                             f'Defisit <b>{fmt_rp(abs(net_balance))}</b>. Kurangi pengeluaran segera!'})
    elif savings_rate >= 20:
        recs.append({'type': 'success', 'icon': '🎉', 'title': f'Arus Kas Sangat Sehat! (+{savings_rate:.0f}%)',
                     'body': f'Surplus {fmt_rp(net_balance)} bulan ini. Alokasikan ke investasi atau tabungan darurat!'})
    else:
        recs.append({'type': 'info', 'icon': '📊', 'title': f'Arus Kas Positif ({savings_rate:.0f}% surplus)',
                     'body': f'Saldo bersih {fmt_rp(net_balance)}. Coba tingkatkan margin tabungan ke minimal 20% pendapatan.'})

    # 2. Debt Ratio
    if debt_ratio > 50:
        recs.append({'type': 'danger', 'icon': '💳', 'title': f'Beban Hutang Sangat Tinggi ({debt_ratio:.0f}%)',
                     'body': f'Cicilan menyerap {debt_ratio:.0f}% pendapatan. Idealnya < 30%. Pertimbangkan restrukturisasi hutang.'})
    elif debt_ratio > 30:
        recs.append({'type': 'warning', 'icon': '⚠️', 'title': f'Beban Hutang Cukup Tinggi ({debt_ratio:.0f}%)',
                     'body': f'Cicilan menyerap {debt_ratio:.0f}% pendapatan. Batasi hutang baru, fokus lunasi yang ada.'})
    elif active_debts:
        recs.append({'type': 'success', 'icon': '✅', 'title': f'Beban Hutang Sehat ({debt_ratio:.0f}%)',
                     'body': 'Rasio cicilan dalam batas aman. Manfaatkan surplus untuk investasi dan dana darurat.'})

    # 3. Prioritas Hutang
    if active_debts:
        priority = max(active_debts, key=lambda d: d.remaining_amount)
        recs.append({'type': 'info', 'icon': '🎯', 'title': f'Prioritaskan: {priority.name}',
                     'body': f'Hutang terbesar "{priority.name}" sisa {fmt_rp(priority.remaining_amount)}. '
                             f'Bayar ini lebih dulu (strategi Avalanche) untuk bebas hutang lebih cepat.'})

    # 4. Estimasi Bebas Hutang
    if active_debts:
        max_payoff = max((d.estimated_payoff for d in active_debts if d.estimated_payoff), default=None)
        if max_payoff:
            months_to_free = (max_payoff.year - today.year) * 12 + (max_payoff.month - today.month)
            recs.append({'type': 'info', 'icon': '🏁', 'title': f'Estimasi Bebas Hutang: {max_payoff.strftime("%b %Y")}',
                         'body': f'Bayar tepat waktu → bebas hutang ~{months_to_free} bulan lagi. '
                                 f'Bayar melebihi minimum bisa mempercepat waktu pelunasan.'})

    # 5. Tabungan
    if savings_rate < 10 and net_balance >= 0:
        suggested = actual_income * 0.2
        recs.append({'type': 'warning', 'icon': '🛡️', 'title': 'Tingkat Tabungan Masih Rendah',
                     'body': f'Sisihkan minimal 10–20% pendapatan untuk dana darurat/tabungan. '
                             f'Target: {fmt_rp(suggested)}/bulan (20%).'})

    # 6. Aturan 50/30/20
    recs.append({'type': 'tip', 'icon': '📐', 'title': 'Panduan Alokasi 50/30/20',
                 'body': f'Dari {fmt_rp(actual_income)}: '
                         f'<b>50%</b> kebutuhan ({fmt_rp(actual_income * 0.5)}), '
                         f'<b>30%</b> keinginan ({fmt_rp(actual_income * 0.3)}), '
                         f'<b>20%</b> tabungan ({fmt_rp(actual_income * 0.2)}).'})
    return recs

# ─── App Setup ─────────────────────────────────────────────

app = Flask(__name__)
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'debuapp-secret-key-change-in-prod'
db.init_app(app)

def migrate_and_init_db():
    with app.app_context():
        db.create_all()
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        if 'debt' in tables:
            debt_cols = [c['name'] for c in inspector.get_columns('debt')]
            for col, coltype in [('category', "VARCHAR(50) DEFAULT 'Lainnya'"),
                                  ('notes', 'TEXT'), ('created_at', 'DATETIME')]:
                if col not in debt_cols:
                    with db.engine.connect() as conn:
                        conn.execute(text(f'ALTER TABLE debt ADD COLUMN {col} {coltype}'))
                        if col == 'created_at':
                            conn.execute(text("UPDATE debt SET created_at=datetime('now') WHERE created_at IS NULL"))
                        conn.commit()

        if 'transaction' in tables and 'cash_transaction' in tables:
            with db.engine.connect() as conn:
                old_cols = [c['name'] for c in inspector.get_columns('transaction')]
                cols = ', '.join(c for c in ['id','date','month','year','type','category',
                                             'amount','description','created_at'] if c in old_cols)
                rows = conn.execute(text(f'SELECT {cols} FROM "transaction"')).fetchall()
                for row in rows:
                    d = dict(row._mapping)
                    if not conn.execute(text('SELECT id FROM cash_transaction WHERE id=:id'), {'id': d['id']}).fetchone():
                        conn.execute(text(
                            "INSERT INTO cash_transaction (id,date,month,year,type,category,amount,description,payment_method,created_at) "
                            "VALUES (:id,:date,:month,:year,:type,:category,:amount,:description,'cash',:created_at)"), d)
                conn.commit()

        # Migrasi kolom baru di cash_transaction
        if 'cash_transaction' in tables:
            ct_cols = [c['name'] for c in inspector.get_columns('cash_transaction')]
            for col, coltype in [('is_recurring', 'BOOLEAN DEFAULT 0'),
                                  ('transfer_to_wallet', 'VARCHAR(50)')]:
                if col not in ct_cols:
                    with db.engine.connect() as conn:
                        conn.execute(text(f'ALTER TABLE cash_transaction ADD COLUMN {col} {coltype}'))
                        conn.commit()

        if WalletAccount.query.count() == 0:
            db.session.add_all([
                WalletAccount(name='Dompet Tunai', method='cash'),
                WalletAccount(name='GoPay',        method='digital'),
                WalletAccount(name='OVO',          method='digital'),
                WalletAccount(name='Bank (Umum)',   method='digital'),
            ])
            db.session.commit()

migrate_and_init_db()

# ─── Dashboard ─────────────────────────────────────────────

@app.route('/')
def dashboard():
    today = date.today()
    debts = Debt.query.all()
    for d in debts:
        enrich_debt(d, today)

    active_debts = [d for d in debts if not d.not_started and d.remaining_amount > 0]
    unpaid = [d for d in active_debts if not d.is_paid_this_month]
    total_installment = sum(d.monthly_installment for d in unpaid)
    total_debt_remaining = sum(d.remaining_amount for d in debts)
    paid_count = sum(1 for d in active_debts if d.is_paid_this_month)

    actual_income, actual_expense, txs = get_monthly_cashflow(today.month, today.year)
    net_balance = actual_income - actual_expense - total_installment
    wallet = get_wallet_balances(txs)

    budget_pct = min(100, round(total_installment / actual_income * 100, 1)) if actual_income > 0 else 0
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    daily_net = net_balance / days_in_month if net_balance > 0 else 0

    trend_data  = json.dumps(get_trend_data(6))
    mom         = get_mom_comparison()
    sp_alerts   = get_spending_alerts(today.month, today.year)

    recommendations = generate_recommendations(
        actual_income, total_installment, actual_expense, debts, today
    )

    return render_template('dashboard.html',
        active_page='dashboard',
        debts=debts, today=today,
        actual_income=actual_income,
        actual_expense=actual_expense,
        total_installment=total_installment,
        total_debt=total_debt_remaining,
        net_balance=net_balance,
        wallet=wallet,
        daily_net=daily_net,
        paid_count=paid_count, total_active=len(active_debts),
        month_name=MONTH_NAMES[today.month - 1], year=today.year,
        budget_pct=budget_pct,
        has_transactions=(len(txs) > 0),
        trend_data=trend_data, mom=mom, sp_alerts=sp_alerts,
        recommendations=recommendations,
    )

# ─── Transactions ──────────────────────────────────────────

@app.route('/transaksi', methods=['GET', 'POST'])
def transactions_page():
    today = date.today()
    view_month = int(request.args.get('month', today.month))
    view_year  = int(request.args.get('year',  today.year))

    wallets = WalletAccount.query.order_by(WalletAccount.method, WalletAccount.name).all()
    cash_wallets    = [w for w in wallets if w.method == 'cash']
    digital_wallets = [w for w in wallets if w.method == 'digital']

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'add':
            try:
                tx_type = request.form.get('type')
                category = request.form.get('category', 'Lainnya')
                amount = float(request.form.get('amount', 0))
                description = request.form.get('description', '').strip()
                payment_method = request.form.get('payment_method', 'cash')
                wallet_name = request.form.get('wallet_name', '').strip() or None
                is_recurring = request.form.get('is_recurring') == 'on'
                date_str = request.form.get('date')
                tx_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today
                if amount > 0 and tx_type in ('income', 'expense'):
                    db.session.add(Transaction(
                        date=tx_date, month=tx_date.month, year=tx_date.year,
                        type=tx_type, category=category, amount=amount,
                        description=description, payment_method=payment_method,
                        wallet_name=wallet_name, is_recurring=is_recurring,
                    ))
                    db.session.commit()
                    label = 'Pemasukan' if tx_type == 'income' else 'Pengeluaran'
                    flash(f'✅ {label} "{category}" Rp {amount:,.0f} berhasil dicatat!', 'success')
                else:
                    flash('⚠️ Jumlah harus > 0 dan tipe harus dipilih.', 'error')
            except ValueError:
                flash('⚠️ Input tidak valid.', 'error')
            return redirect(url_for('transactions_page', month=view_month, year=view_year))

        elif action == 'transfer':
            try:
                amount = float(request.form.get('amount', 0))
                src_wallet = request.form.get('wallet_name', '').strip()
                tgt_wallet = request.form.get('transfer_to_wallet', '').strip()
                description = request.form.get('description', '').strip()
                date_str = request.form.get('date')
                tx_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today
                src = WalletAccount.query.filter_by(name=src_wallet).first()
                if amount > 0 and src and tgt_wallet:
                    db.session.add(Transaction(
                        date=tx_date, month=tx_date.month, year=tx_date.year,
                        type='transfer', category='Transfer', amount=amount,
                        description=description or f'{src_wallet} → {tgt_wallet}',
                        payment_method=src.method, wallet_name=src_wallet,
                        transfer_to_wallet=tgt_wallet,
                    ))
                    db.session.commit()
                    flash(f'✅ Transfer Rp {amount:,.0f} dari {src_wallet} ke {tgt_wallet} berhasil!', 'success')
                else:
                    flash('⚠️ Isi semua field transfer dengan benar.', 'error')
            except ValueError:
                flash('⚠️ Input tidak valid.', 'error')
            return redirect(url_for('transactions_page', month=view_month, year=view_year))

        elif action == 'apply_recurring':
            copied = apply_recurring(view_month, view_year)
            if copied > 0:
                flash(f'✅ {copied} transaksi berulang berhasil diterapkan ke bulan ini!', 'success')
            else:
                flash('ℹ️ Tidak ada transaksi berulang baru untuk diterapkan.', 'info')
            return redirect(url_for('transactions_page', month=view_month, year=view_year))

        elif action == 'set_limit':
            try:
                cat = request.form.get('limit_category')
                lamt = float(request.form.get('limit_amount', 0))
                if cat and lamt > 0:
                    existing = SpendingLimit.query.filter_by(category=cat, month=view_month, year=view_year).first()
                    if existing:
                        existing.limit_amount = lamt
                    else:
                        db.session.add(SpendingLimit(category=cat, limit_amount=lamt,
                                                     month=view_month, year=view_year))
                    db.session.commit()
                    flash(f'✅ Batas pengeluaran "{cat}" = Rp {lamt:,.0f} disimpan!', 'success')
            except ValueError:
                flash('⚠️ Input tidak valid.', 'error')
            return redirect(url_for('transactions_page', month=view_month, year=view_year))

        elif action == 'delete':
            try:
                tx_id = int(request.form.get('tx_id'))
                tx = db.session.get(Transaction, tx_id)
                if tx:
                    db.session.delete(tx)
                    db.session.commit()
                    flash('🗑️ Transaksi berhasil dihapus.', 'info')
            except (ValueError, TypeError):
                pass
            return redirect(url_for('transactions_page', month=view_month, year=view_year))

        elif action == 'add_wallet':
            name   = request.form.get('wallet_name_new', '').strip()
            method = request.form.get('wallet_method', 'cash')
            notes  = request.form.get('wallet_notes', '').strip()
            if name:
                exists = WalletAccount.query.filter_by(name=name).first()
                if not exists:
                    db.session.add(WalletAccount(name=name, method=method, notes=notes))
                    db.session.commit()
                    flash(f'✅ Dompet "{name}" berhasil ditambahkan!', 'success')
                else:
                    flash(f'⚠️ Dompet "{name}" sudah ada.', 'error')
            return redirect(url_for('transactions_page', month=view_month, year=view_year))

        elif action == 'delete_wallet':
            try:
                wid = int(request.form.get('wallet_id'))
                w = db.session.get(WalletAccount, wid)
                if w:
                    db.session.delete(w)
                    db.session.commit()
                    flash(f'🗑️ Dompet "{w.name}" dihapus.', 'info')
            except (ValueError, TypeError):
                pass
            return redirect(url_for('transactions_page', month=view_month, year=view_year))

    actual_income, actual_expense, all_txs = get_monthly_cashflow(view_month, view_year)
    wallet = get_wallet_balances(all_txs)

    debts = Debt.query.all()
    for d in debts:
        enrich_debt(d, today)
    unpaid = [d for d in debts if not d.not_started and d.remaining_amount > 0 and not d.is_paid_this_month]
    total_installment = sum(d.monthly_installment for d in unpaid)
    net_balance = actual_income - actual_expense - total_installment

    sorted_txs = sorted(all_txs, key=lambda t: (t.date, t.created_at), reverse=True)

    income_breakdown  = {}
    expense_breakdown = {}
    for tx in all_txs:
        if tx.type == 'income':
            income_breakdown[tx.category]  = income_breakdown.get(tx.category, 0) + tx.amount
        elif tx.type == 'expense':
            expense_breakdown[tx.category] = expense_breakdown.get(tx.category, 0) + tx.amount

    cumulative_wallets = get_all_wallet_balances()

    # Spending limits dengan aktual
    limits = SpendingLimit.query.filter_by(month=view_month, year=view_year).all()
    for lim in limits:
        lim.spent = expense_breakdown.get(lim.category, 0)
        lim.pct   = min(100, round(lim.spent / lim.limit_amount * 100, 1)) if lim.limit_amount > 0 else 0

    has_recurring_prev = Transaction.query.filter_by(
        month=(view_month-1 if view_month > 1 else 12),
        year=(view_year if view_month > 1 else view_year-1),
        is_recurring=True).count() > 0

    return render_template('transactions.html',
        active_page='transactions',
        today=today,
        view_month=view_month, view_year=view_year,
        month_name=MONTH_NAMES[view_month - 1],
        months=MONTH_NAMES,
        transactions=sorted_txs,
        actual_income=actual_income,
        actual_expense=actual_expense,
        total_installment=total_installment,
        net_balance=net_balance,
        wallet=wallet,
        income_categories=INCOME_CATEGORIES,
        expense_categories=EXPENSE_CATEGORIES,
        income_breakdown=income_breakdown,
        expense_breakdown=expense_breakdown,
        cash_wallets=cash_wallets,
        digital_wallets=digital_wallets,
        all_wallets=wallets,
        cumulative_wallets=cumulative_wallets,
        limits=limits,
        has_recurring_prev=has_recurring_prev,
    )


# ─── Debt Management ───────────────────────────────────────

@app.route('/debts', methods=['GET', 'POST'])
def debts_page():
    today = date.today()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            name = request.form.get('name', '').strip()
            category = request.form.get('category', 'Lainnya')
            notes = request.form.get('notes', '').strip()
            try:
                total = float(request.form.get('total_amount', 0))
                tenure = int(request.form.get('tenure_months', 1))
                start_str = request.form.get('start_date')
                start_date_val = datetime.strptime(start_str, '%Y-%m-%d').date() if start_str else today
                if tenure > 0 and name and total > 0:
                    new_debt = Debt(
                        name=name, category=category, notes=notes,
                        total_amount=total, remaining_amount=total,
                        tenure_months=tenure, monthly_installment=total / tenure,
                        start_date=start_date_val, paid_until=add_months(start_date_val, -1)
                    )
                    db.session.add(new_debt)
                    db.session.commit()
                    flash(f'✅ Hutang "{name}" berhasil ditambahkan!', 'success')
                else:
                    flash('⚠️ Nama, jumlah, dan tenor wajib diisi.', 'error')
            except ValueError:
                flash('⚠️ Input tidak valid.', 'error')
        elif action == 'pay':
            try:
                debt_id = int(request.form.get('debt_id'))
                pay_months = int(request.form.get('pay_months', 1))
                debt = db.session.get(Debt, debt_id)
                if debt and pay_months > 0:
                    if debt.paid_until is None:
                        debt.paid_until = add_months(debt.start_date, -1)
                    debt.paid_until = add_months(debt.paid_until, pay_months)
                    debt.remaining_amount = max(0, debt.remaining_amount - (debt.monthly_installment * pay_months))
                    db.session.commit()
                    flash(f'✅ Pembayaran {pay_months} bulan untuk "{debt.name}" berhasil!', 'success')
            except (ValueError, TypeError):
                flash('⚠️ Kesalahan saat memproses pembayaran.', 'error')
        elif action == 'delete':
            try:
                debt_id = int(request.form.get('debt_id'))
                debt = db.session.get(Debt, debt_id)
                if debt:
                    name = debt.name
                    db.session.delete(debt)
                    db.session.commit()
                    flash(f'🗑️ Hutang "{name}" berhasil dihapus.', 'info')
            except (ValueError, TypeError):
                flash('⚠️ Kesalahan saat menghapus hutang.', 'error')
        return redirect(url_for('debts_page'))

    all_debts = Debt.query.order_by(Debt.start_date.desc()).all()
    for d in all_debts:
        enrich_debt(d, today)
    return render_template('debts.html', active_page='debts',
                           debts=all_debts, today=today, categories=DEBT_CATEGORIES)

@app.route('/debts/edit/<int:debt_id>', methods=['GET', 'POST'])
def edit_debt(debt_id):
    debt = db.session.get(Debt, debt_id)
    if not debt:
        flash('Hutang tidak ditemukan.', 'error')
        return redirect(url_for('debts_page'))
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            category = request.form.get('category', 'Lainnya')
            notes = request.form.get('notes', '').strip()
            tenure = int(request.form.get('tenure_months', 1))
            remaining = float(request.form.get('remaining_amount', 0))
            if name and tenure > 0:
                debt.name = name; debt.category = category; debt.notes = notes
                debt.tenure_months = tenure
                debt.remaining_amount = max(0, remaining)
                debt.monthly_installment = debt.total_amount / tenure
                db.session.commit()
                flash(f'✅ Hutang "{name}" berhasil diperbarui!', 'success')
                return redirect(url_for('debts_page'))
            else:
                flash('⚠️ Nama dan tenor wajib diisi.', 'error')
        except ValueError:
            flash('⚠️ Input tidak valid.', 'error')
    return render_template('edit_debt.html', active_page='debts',
                           debt=debt, categories=DEBT_CATEGORIES)

# ─── Export CSV ────────────────────────────────────────────

@app.route('/export')
def export_csv():
    month = int(request.args.get('month', date.today().month))
    year  = int(request.args.get('year',  date.today().year))
    txs = Transaction.query.filter_by(month=month, year=year).order_by(Transaction.date).all()
    si = StringIO()
    w = csv.writer(si)
    w.writerow(['Tanggal','Tipe','Kategori','Metode','Dompet','Jumlah','Keterangan','Berulang'])
    for t in txs:
        w.writerow([t.date, t.type, t.category, t.payment_method,
                    t.wallet_name or '', t.amount, t.description or '', t.is_recurring])
    fname = f'transaksi_{MONTH_NAMES[month-1]}_{year}.csv'
    return Response(si.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename={fname}'})

# ─── Goals ─────────────────────────────────────────────────

@app.route('/goals', methods=['GET', 'POST'])
def goals_page():
    today = date.today()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            try:
                name = request.form.get('name', '').strip()
                target = float(request.form.get('target_amount', 0))
                current = float(request.form.get('current_amount', 0))
                tdate_str = request.form.get('target_date', '')
                tdate = datetime.strptime(tdate_str, '%Y-%m-%d').date() if tdate_str else None
                notes = request.form.get('notes', '').strip()
                if name and target > 0:
                    db.session.add(SavingsGoal(name=name, target_amount=target,
                        current_amount=current, target_date=tdate, notes=notes))
                    db.session.commit()
                    flash(f'✅ Goal "{name}" berhasil ditambahkan!', 'success')
            except ValueError:
                flash('⚠️ Input tidak valid.', 'error')
        elif action == 'update':
            try:
                gid = int(request.form.get('goal_id'))
                g = db.session.get(SavingsGoal, gid)
                if g:
                    g.current_amount = float(request.form.get('current_amount', g.current_amount))
                    db.session.commit()
                    flash('✅ Progress goal diperbarui!', 'success')
            except (ValueError, TypeError): pass
        elif action == 'delete':
            try:
                gid = int(request.form.get('goal_id'))
                g = db.session.get(SavingsGoal, gid)
                if g:
                    db.session.delete(g); db.session.commit()
                    flash(f'🗑️ Goal "{g.name}" dihapus.', 'info')
            except (ValueError, TypeError): pass
        return redirect(url_for('goals_page'))

    goals = SavingsGoal.query.filter_by(is_active=True).order_by(SavingsGoal.created_at).all()
    for g in goals:
        g.progress_pct = min(100, round(g.current_amount / g.target_amount * 100, 1)) if g.target_amount > 0 else 0
        remaining = max(0, g.target_amount - g.current_amount)
        if g.target_date:
            ml = (g.target_date.year - today.year)*12 + (g.target_date.month - today.month)
            g.months_left = ml
            g.monthly_needed = round(remaining / ml, 0) if ml > 0 else remaining
        else:
            g.months_left = None; g.monthly_needed = None
    return render_template('goals.html', active_page='goals', goals=goals, today=today)

# ─── Net Worth ──────────────────────────────────────────────

@app.route('/networth', methods=['GET', 'POST'])
def networth_page():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            try:
                name = request.form.get('name', '').strip()
                cat  = request.form.get('category', 'Lainnya')
                val  = float(request.form.get('value', 0))
                notes = request.form.get('notes', '').strip()
                if name and val > 0:
                    db.session.add(Asset(name=name, category=cat, value=val, notes=notes))
                    db.session.commit()
                    flash(f'✅ Aset "{name}" berhasil ditambahkan!', 'success')
            except ValueError:
                flash('⚠️ Input tidak valid.', 'error')
        elif action == 'update':
            try:
                aid = int(request.form.get('asset_id'))
                a = db.session.get(Asset, aid)
                if a:
                    a.value = float(request.form.get('value', a.value))
                    a.notes = request.form.get('notes', a.notes)
                    db.session.commit()
                    flash('✅ Nilai aset diperbarui!', 'success')
            except (ValueError, TypeError): pass
        elif action == 'delete':
            try:
                aid = int(request.form.get('asset_id'))
                a = db.session.get(Asset, aid)
                if a:
                    db.session.delete(a); db.session.commit()
                    flash(f'🗑️ Aset "{a.name}" dihapus.', 'info')
            except (ValueError, TypeError): pass
        return redirect(url_for('networth_page'))

    assets = Asset.query.order_by(Asset.category, Asset.name).all()
    total_assets = sum(a.value for a in assets)
    total_liabilities = sum(d.remaining_amount for d in Debt.query.all())
    net_worth = total_assets - total_liabilities
    by_cat = {}
    for a in assets:
        by_cat.setdefault(a.category, []).append(a)
    return render_template('networth.html', active_page='networth',
        assets=assets, total_assets=total_assets,
        total_liabilities=total_liabilities, net_worth=net_worth,
        by_cat=by_cat, asset_categories=ASSET_CATEGORIES)

# ─── Simulasi Budget ────────────────────────────────────────

@app.route('/simulasi')
def simulasi_page():
    today = date.today()

    # ── Data aktual bulan ini ──
    actual_income, actual_expense, txs = get_monthly_cashflow(today.month, today.year)

    debts = Debt.query.all()
    for d in debts:
        enrich_debt(d, today)

    active_debts = [d for d in debts if not d.not_started and d.remaining_amount > 0]
    unpaid = [d for d in active_debts if not d.is_paid_this_month]
    total_installment = sum(d.monthly_installment for d in unpaid)

    saldo_bersih = actual_income - actual_expense - total_installment

    # ── Input simulasi dari query params ──
    extra_income = float(request.args.get('extra_income', 0))

    # ── Hitung sisa hari (besok s.d akhir bulan) ──
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    remaining_days = days_in_month - today.day
    if remaining_days < 1:
        remaining_days = 1  # fallback jika hari terakhir

    # ── Budget harian & mingguan ──
    available = saldo_bersih + extra_income
    daily_budget = available / remaining_days if remaining_days > 0 else 0
    weekly_budget = daily_budget * 7

    # ── Proyeksi 3 bulan ke depan ──
    # Asumsi: pola pemasukan & pengeluaran tetap, hutang berkurang per bulan
    projection = []
    proj_income = actual_income + extra_income  # asumsi pemasukan stabil
    proj_expense = actual_expense  # asumsi pengeluaran stabil

    proj_debts_remaining = sum(d.remaining_amount for d in debts if d.remaining_amount > 0)
    proj_monthly_installment = sum(d.monthly_installment for d in active_debts)

    for i in range(1, 4):
        pm = today.month + i
        py = today.year
        while pm > 12:
            pm -= 12
            py += 1

        # Hutang berkurang tiap bulan
        proj_debts_remaining = max(0, proj_debts_remaining - proj_monthly_installment)
        # Jika hutang sudah 0, cicilan juga 0
        eff_installment = min(proj_monthly_installment, proj_debts_remaining + proj_monthly_installment)

        proj_saldo = proj_income - proj_expense - eff_installment
        projection.append({
            'label': f'{MONTH_NAMES[pm-1]} {py}',
            'income': proj_income,
            'expense': proj_expense,
            'installment': eff_installment,
            'saldo': proj_saldo,
            'debt_remaining': proj_debts_remaining,
        })

    # ── Timeline hutang lunas ──
    debt_timeline = []
    total_remaining_payments = 0
    for d in debts:
        if d.remaining_amount > 0 and d.monthly_installment > 0:
            months_left = math.ceil(d.remaining_amount / d.monthly_installment)
            payoff_date = add_months(today, months_left)
            total_remaining_payments += d.remaining_amount
            debt_timeline.append({
                'name': d.name,
                'remaining': d.remaining_amount,
                'installment': d.monthly_installment,
                'months_left': months_left,
                'payoff_date': payoff_date,
            })
    debt_timeline.sort(key=lambda x: x['months_left'])

    # ── Tips otomatis ──
    tips = []
    if daily_budget < 0:
        tips.append({'type': 'danger', 'icon': '🚨',
                     'text': f'Budget harian negatif! Anda sudah melebihi anggaran bulan ini sebesar Rp {abs(available):,.0f}. Kurangi pengeluaran segera.'})
    elif daily_budget < 30000:
        tips.append({'type': 'warning', 'icon': '⚠️',
                     'text': f'Budget harian Anda sangat ketat (Rp {daily_budget:,.0f}/hari). Prioritaskan kebutuhan pokok saja.'})
    elif daily_budget < 75000:
        tips.append({'type': 'info', 'icon': '💡',
                     'text': f'Budget harian Anda cukup (Rp {daily_budget:,.0f}/hari). Tetap bijak dalam pengeluaran.'})
    else:
        tips.append({'type': 'success', 'icon': '✅',
                     'text': f'Budget harian Anda sehat (Rp {daily_budget:,.0f}/hari). Sisihkan sebagian untuk tabungan!'})

    if total_installment > 0 and actual_income > 0:
        debt_ratio = total_installment / actual_income * 100
        if debt_ratio > 40:
            tips.append({'type': 'danger', 'icon': '💳',
                         'text': f'Cicilan menyerap {debt_ratio:.0f}% pendapatan. Hindari hutang baru dan fokus pelunasan.'})

    if extra_income == 0 and saldo_bersih < actual_income * 0.2:
        tips.append({'type': 'info', 'icon': '🔮',
                     'text': 'Coba masukkan estimasi pemasukan tambahan di atas untuk melihat dampaknya terhadap budget harian.'})

    return render_template('simulasi.html',
        active_page='simulasi',
        today=today,
        month_name=MONTH_NAMES[today.month - 1],
        year=today.year,
        actual_income=actual_income,
        actual_expense=actual_expense,
        total_installment=total_installment,
        saldo_bersih=saldo_bersih,
        extra_income=extra_income,
        available=available,
        remaining_days=remaining_days,
        daily_budget=daily_budget,
        weekly_budget=weekly_budget,
        projection=projection,
        debt_timeline=debt_timeline,
        total_remaining_payments=total_remaining_payments,
        tips=tips,
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000)
