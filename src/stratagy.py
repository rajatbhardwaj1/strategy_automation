def execute_strategy(all_etfs, held_etfs_df, etf_to_average, book_today, cash, order_value, api):
    """
    Place buy/sell orders according to strategy
    """
    # Sell ETFs for trailing profit
    for _, row in book_today.iterrows():
        api.place_order({
            'tradingsymbol': row['etf'],
            'transactiontype': 'SELL',
            'quantity': int(row['held_qty']),
            'ordertype': 'STOPLOSS_MARKET'
        })

    # Buy new ETFs if cash available
    all_etfs_sorted = all_etfs.sort_values(by='Difference')
    for _, row in all_etfs_sorted.iterrows():
        if row['etf'] not in held_etfs_df['etf'].values and cash >= order_value:
            qty = int(order_value / row['ltp'])
            api.place_order({
                'tradingsymbol': row['etf'],
                'transactiontype': 'BUY',
                'quantity': qty,
                'ordertype': 'LIMIT',
                'price': row['ltp']
            })
            cash -= qty * row['ltp']
