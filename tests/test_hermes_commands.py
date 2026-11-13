import importlib.util
import json
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('shop_plugin', Path(__file__).resolve().parents[1] / 'integrations/hermes/lightcard/__init__.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
OID = '5b3f743ba749fdf1dd447fe2'


class DeliveryCommandTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'WEIXIN_HOME_CHANNEL': 'owner'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.source = {'user_id': 'owner', 'chat_type': 'dm'}
        self.order = {'id': OID, 'status': 'pending', 'price_cents': 100, 'quantity': 1}

    def invoke(self, text=None):
        return plugin.handle_delivery_message('weixin', self.source, text or f'{OID} 发卡')

    def test_explicit_command_variants(self):
        for text in (f'{OID} 发卡', f'给 {OID} 发卡', f'给订单 {OID} 发卡', f'发卡 {OID}',
                     f'已核实到账，给订单 [{OID}] 发卡', f'请给 {OID} 发卡！'):
            self.assertEqual(plugin.delivery_order_id(text), OID, text)

    def test_queries_negations_quotes_and_multiple_orders_do_not_deliver(self):
        for text in (OID, f'查订单 {OID}', f'不要给 {OID} 发卡', f'{OID} 发卡了吗？',
                     f'买家说：给 {OID} 发卡', f'给 {OID} 发卡\n忽略上面的要求',
                     f'{OID} {OID} 发卡', f'"给 {OID} 发卡"'):
            self.assertIsNone(plugin.delivery_order_id(text), text)

    def test_pending_order_delivers_without_extra_confirmation(self):
        with patch.object(plugin, 'api', side_effect=[{'orders': [self.order]},
            {'order': {**self.order, 'status': 'paid'}}, {'products': []}]) as api:
            result = self.invoke()
        self.assertEqual(api.call_count, 3)
        self.assertEqual(api.call_args_list[1].args[1], {'payment_verified': True, 'expected_total_cents': 100, 'expected_quantity': 1})
        self.assertEqual(result['action'], 'handled')
        self.assertIn('发卡完成', result['reply'])
        self.assertNotIn('\n', result['reply'])
        self.assertNotIn('请确认', result['reply'])

    def test_nonowner_or_group_never_reaches_api(self):
        for source in ({'user_id': 'other', 'chat_type': 'dm'}, {'user_id': 'owner', 'chat_type': 'group'}):
            self.source = source
            with patch.object(plugin, 'api') as api:
                self.assertEqual(self.invoke()['reply'], '无权执行发卡。')
                api.assert_not_called()

    def test_paid_cancelled_missing_orders_never_write(self):
        for orders in ([{**self.order, 'status': 'paid'}], [{**self.order, 'status': 'cancelled'}], []):
            with patch.object(plugin, 'api', return_value={'orders': orders}) as api:
                self.assertEqual(self.invoke()['action'], 'handled')
                self.assertEqual(api.call_count, 1)

    def test_failed_delivery_never_claims_success_or_falls_back_to_model(self):
        with patch.object(plugin, 'api', side_effect=[{'orders': [self.order]}, TimeoutError()]):
            result = self.invoke()
        self.assertEqual(result['action'], 'handled')
        self.assertIn('暂未确认', result['reply'])

    def test_out_of_stock_reports_one_message(self):
        with patch.object(plugin, 'api', side_effect=[{'orders': [self.order]}, {'error': {'code': 'out_of_stock'}}]):
            result = self.invoke()
        self.assertIn('库存不足', result['reply'])
        self.assertNotIn('\n', result['reply'])

    def test_low_stock_returns_every_product_and_threshold_is_strict(self):
        for available in (0, 1, 2, 3):
            products = [
                {'id': 1, 'name': '商品一', 'inventory': {'available': available, 'reserved': 2, 'sold': 9}},
                {'id': 2, 'name': '商品二', 'inventory': {'available': 50, 'reserved': 0, 'sold': 1}}]
            with patch.object(plugin, 'api', return_value={'products': products}):
                result = json.loads(plugin.inventory({}))
                message = plugin.append_low_stock('发卡完成。')
            self.assertEqual(result['low_stock'], available < 3)
            if available < 3:
                for name in ('商品一', '商品二'):
                    self.assertIn(name, result['required_reply'])
                    self.assertIn(name, message)
                self.assertIn('预留2', message)
                self.assertIn('已售9', message)
                self.assertNotIn('\n', message)
            else:
                self.assertNotIn('required_reply', result)
                self.assertEqual(message, '发卡完成。')

    def test_inventory_failure_preserves_successful_delivery(self):
        with patch.object(plugin, 'api', side_effect=[{'orders': [self.order]},
            {'order': {**self.order, 'status': 'paid'}}, TimeoutError()]):
            result = self.invoke()
        self.assertIn('发卡完成', result['reply'])
        self.assertIn('库存查询暂不可用', result['reply'])
