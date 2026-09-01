"""Minimal i18n: a flat key -> {lang: template} dict and a t() lookup.

Scope, deliberately: this translates the highest-traffic user-facing
surface - the main menu and the purchase flow - not the whole bot. The
admin panel stays English (the admin already reads English; it's their own
store). Extending coverage means adding more keys here and swapping the
matching f-strings in handlers for t() calls; nothing about the mechanism
itself is Bengali-specific or purchase-flow-specific.

Usage: `t('main_menu.title', lang, name=user.username)` - lang falls back
to 'en' if the key or the language itself isn't found, so a missing
translation degrades to English rather than raising or showing a raw key.
"""

DEFAULT_LANG = 'en'
SUPPORTED_LANGS = ('en', 'bn')

_TRANSLATIONS = {
    'main_menu.wallet_balance': {
        'en': "💰 Your Wallet Balance: {balance}",
        'bn': "💰 আপনার ওয়ালেট ব্যালেন্স: {balance}",
    },
    'main_menu.button.products': {
        'en': "🛒 Products",
        'bn': "🛒 প্রোডাক্ট",
    },
    'main_menu.button.wallet': {
        'en': "💰 Wallet",
        'bn': "💰 ওয়ালেট",
    },
    'main_menu.button.topup': {
        'en': "💳 Top Up",
        'bn': "💳 টপ আপ",
    },
    'main_menu.button.order_history': {
        'en': "📋 Order History",
        'bn': "📋 অর্ডার হিস্টরি",
    },
    'main_menu.button.support': {
        'en': "💬 Support",
        'bn': "💬 সাপোর্ট",
    },
    'main_menu.button.availability': {
        'en': "📦 Availability",
        'bn': "📦 স্টক দেখুন",
    },
    'main_menu.button.language': {
        'en': "🌐 বাংলা",  # shown to an English-reading user as the offer to switch
        'bn': "🌐 English",  # shown to a Bengali-reading user as the offer to switch
    },
    'purchase.quantity_prompt': {
        'en': "🛒 Purchase: {product_name}\n\n"
              "💰 Price: {price} each\n"
              "📦 Available: {stock}\n\n"
              "💬 Please enter the quantity you want to buy (1-{stock}):",
        'bn': "🛒 কিনুন: {product_name}\n\n"
              "💰 দাম: {price} প্রতিটি\n"
              "📦 স্টকে আছে: {stock}\n\n"
              "💬 কত পরিমাণ কিনতে চান লিখুন (১-{stock}):",
    },
    'purchase.confirm_title': {
        'en': "🛒 Confirm Purchase\n\n"
              "📦 Product: {product_name}\n"
              "💰 Price: {price} x {quantity}\n"
              "💵 Total: {total}",
        'bn': "🛒 কেনাকাটা নিশ্চিত করুন\n\n"
              "📦 প্রোডাক্ট: {product_name}\n"
              "💰 দাম: {price} x {quantity}\n"
              "💵 মোট: {total}",
    },
    'purchase.insufficient_balance': {
        'en': "⚠️ Insufficient Balance!\n💰 Your Wallet Balance: {balance}\n\n💡 Please top up your wallet first.",
        'bn': "⚠️ পর্যাপ্ত ব্যালেন্স নেই!\n💰 আপনার ওয়ালেট ব্যালেন্স: {balance}\n\n💡 আগে ওয়ালেটে টাকা যোগ করুন।",
    },
    'purchase.success': {
        'en': "✅ Purchase Successful!\n\n💰 Total Amount: {total}\n📝 Order ID: #{order_id}\n\n{details}\nThank you for your purchase!",
        'bn': "✅ কেনাকাটা সফল হয়েছে!\n\n💰 মোট: {total}\n📝 অর্ডার আইডি: #{order_id}\n\n{details}\nধন্যবাদ!",
    },
    'purchase.cancelled': {
        'en': "❌ Purchase cancelled.",
        'bn': "❌ কেনাকাটা বাতিল হয়েছে।",
    },
    'purchase.button.confirm': {
        'en': "✅ Confirm Purchase",
        'bn': "✅ কেনাকাটা নিশ্চিত করুন",
    },
    'purchase.button.cancel': {
        'en': "❌ Cancel",
        'bn': "❌ বাতিল",
    },
    'purchase.button.topup_wallet': {
        'en': "💰 Top Up Wallet",
        'bn': "💰 ওয়ালেটে টাকা যোগ করুন",
    },
    'purchase.banned': {
        'en': "⛔ You have been banned from using this bot.",
        'bn': "⛔ এই বটে আপনাকে ব্যান করা হয়েছে।",
    },
    'language.prompt': {
        'en': "🌐 Choose your language:",
        'bn': "🌐 আপনার ভাষা বেছে নিন:",
    },
    'language.saved': {
        'en': "✅ Language set to English.",
        'bn': "✅ ভাষা বাংলা করা হয়েছে।",
    },
}


def t(key: str, lang: str = DEFAULT_LANG, **kwargs) -> str:
    """Look up `key` in `lang`, falling back to English, then to the key
    itself (so a typo'd/missing key is visible instead of raising).
    """
    entry = _TRANSLATIONS.get(key)
    if entry is None:
        return key
    template = entry.get(lang) or entry.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template
