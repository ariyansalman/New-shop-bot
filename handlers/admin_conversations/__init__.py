"""Admin conversation handlers for multi-step workflows.

Split into feature modules (products, categories, store_settings,
broadcast, plus the shared cancel_conversation) - this package re-exports
every name the original single-file module exposed, so `bot.py`'s
`admin_conversations.X` references need no changes. See CONTRIBUTING.md.
"""

from ._shared import cancel_conversation

from .products import (
    PRODUCT_NAME, PRODUCT_DESC, PRODUCT_PRICE, PRODUCT_TYPE, PRODUCT_CATEGORY,
    PRODUCT_SUBCATEGORY, PRODUCT_IMAGE, PRODUCT_INSTRUCTIONS,
    PRODUCT_DOWNLOAD_LINK, PRODUCT_KEYS,
    EDIT_SELECT_PRODUCT, EDIT_SELECT_FIELD, EDIT_NEW_VALUE, EDIT_IMAGE_VALUE,
    create_product_start, product_name, product_desc, product_price,
    product_type, product_category, product_subcategory, product_image,
    product_instructions, product_download_link, product_keys, create_product_final,
    cancel_product_creation,
    edit_product_start, edit_select_product, edit_select_field,
    edit_new_value, edit_image_value,
)

from .categories import (
    CATEGORY_NAME, CATEGORY_DESC,
    SUBCATEGORY_CATEGORY, SUBCATEGORY_NAME,
    EDIT_CATEGORY_SELECT, EDIT_CATEGORY_FIELD, EDIT_CATEGORY_VALUE,
    EDIT_SUBCATEGORY_SELECT, EDIT_SUBCATEGORY_FIELD, EDIT_SUBCATEGORY_VALUE,
    create_category_start, category_name, category_desc,
    create_subcategory_start, subcategory_category, subcategory_name,
    edit_category_start, edit_category_select, edit_category_field, edit_category_value,
    edit_subcategory_start, edit_subcategory_select, edit_subcategory_field, edit_subcategory_value,
)

from .store_settings import (
    SETTING_VALUE, WELCOME_MESSAGE, STORE_LOGO, TERMS_TEXT, REFERRAL_BONUS,
    config_support_username, config_channel_username, setting_value,
    config_welcome_message, welcome_message_value,
    config_terms, terms_value,
    config_referral, referral_bonus_value,
    config_store_logo, store_logo_value, cancel_settings,
)

from .broadcast import (
    BROADCAST_TEXT, BROADCAST_IMAGE,
    broadcast_text_start, broadcast_text_message,
    broadcast_image_start, broadcast_image_photo, broadcast_image_text,
    cancel_broadcast,
)

__all__ = [
    'cancel_conversation',
    # products
    'PRODUCT_NAME', 'PRODUCT_DESC', 'PRODUCT_PRICE', 'PRODUCT_TYPE', 'PRODUCT_CATEGORY',
    'PRODUCT_SUBCATEGORY', 'PRODUCT_IMAGE', 'PRODUCT_INSTRUCTIONS',
    'PRODUCT_DOWNLOAD_LINK', 'PRODUCT_KEYS',
    'EDIT_SELECT_PRODUCT', 'EDIT_SELECT_FIELD', 'EDIT_NEW_VALUE', 'EDIT_IMAGE_VALUE',
    'create_product_start', 'product_name', 'product_desc', 'product_price',
    'product_type', 'product_category', 'product_subcategory', 'product_image',
    'product_instructions', 'product_download_link', 'product_keys', 'create_product_final',
    'cancel_product_creation',
    'edit_product_start', 'edit_select_product', 'edit_select_field',
    'edit_new_value', 'edit_image_value',
    # categories / subcategories
    'CATEGORY_NAME', 'CATEGORY_DESC',
    'SUBCATEGORY_CATEGORY', 'SUBCATEGORY_NAME',
    'EDIT_CATEGORY_SELECT', 'EDIT_CATEGORY_FIELD', 'EDIT_CATEGORY_VALUE',
    'EDIT_SUBCATEGORY_SELECT', 'EDIT_SUBCATEGORY_FIELD', 'EDIT_SUBCATEGORY_VALUE',
    'create_category_start', 'category_name', 'category_desc',
    'create_subcategory_start', 'subcategory_category', 'subcategory_name',
    'edit_category_start', 'edit_category_select', 'edit_category_field', 'edit_category_value',
    'edit_subcategory_start', 'edit_subcategory_select', 'edit_subcategory_field', 'edit_subcategory_value',
    # store settings
    'SETTING_VALUE', 'WELCOME_MESSAGE', 'STORE_LOGO', 'TERMS_TEXT', 'REFERRAL_BONUS',
    'config_support_username', 'config_channel_username', 'setting_value',
    'config_welcome_message', 'welcome_message_value',
    'config_terms', 'terms_value',
    'config_referral', 'referral_bonus_value',
    'config_store_logo', 'store_logo_value', 'cancel_settings',
    # broadcast
    'BROADCAST_TEXT', 'BROADCAST_IMAGE',
    'broadcast_text_start', 'broadcast_text_message',
    'broadcast_image_start', 'broadcast_image_photo', 'broadcast_image_text',
    'cancel_broadcast',
]
