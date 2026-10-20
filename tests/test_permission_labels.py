import unittest

from permission_labels import permission_label


class PermissionLabelsTests(unittest.TestCase):
    def test_permissions_in_user_device_have_chinese_names(self):
        expected = {
            "ACCESS_BACKGROUND_LOCATION": "后台访问位置信息",
            "ACCESS_COARSE_LOCATION": "获取大致位置信息",
            "ACCESS_FINE_LOCATION": "获取精确位置信息",
            "CAMERA": "使用相机",
            "POST_NOTIFICATIONS": "发送通知",
        }
        for suffix, chinese in expected.items():
            self.assertEqual(permission_label("android.permission." + suffix),
                             chinese)

    def test_vendor_and_unknown_permissions_keep_original_identifier(self):
        self.assertEqual(permission_label("com.example.permission.SPECIAL"), "")
        self.assertEqual(permission_label("android.permission.FUTURE_FEATURE"), "")


if __name__ == "__main__":
    unittest.main()
