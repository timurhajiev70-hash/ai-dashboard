# -*- coding: utf-8 -*-
"""Тесты полноты строк интерфейса (i18n.py, блок E).

Проверяется только публичный контракт: LANGUAGES, get_strings(lang), missing_keys().
Запуск из папки fokus:  python3 -m unittest tests.test_i18n -v
"""
import os
import re
import sys
import unittest

FOKUS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, FOKUS_DIR)

import i18n  # noqa: E402

PLACEHOLDER = re.compile(r'\{(\w+)\}')


class TestI18n(unittest.TestCase):

    def test_languages(self):
        self.assertEqual(set(i18n.LANGUAGES), {'ru', 'de'}, '§4.2: language ∈ {ru, de}')

    def test_missing_keys_empty(self):
        mk = i18n.missing_keys()
        self.assertIsInstance(mk, dict)
        self.assertEqual(mk.get('ru'), [], 'в ru нет ключей: %s' % mk.get('ru'))
        self.assertEqual(mk.get('de'), [], 'в de нет ключей: %s' % mk.get('de'))

    def test_same_key_sets(self):
        ru = i18n.get_strings('ru')
        de = i18n.get_strings('de')
        self.assertEqual(set(ru), set(de))
        self.assertGreater(len(ru), 0)

    def test_unknown_language_falls_back_to_ru(self):
        ru = i18n.get_strings('ru')
        for lang in ('xx', 'en', '', 'RU', 'De', None):
            with self.subTest(lang=lang):
                self.assertEqual(i18n.get_strings(lang), ru)

    def test_de_is_de(self):
        self.assertIsNot(i18n.get_strings('de'), None)
        self.assertIsInstance(i18n.get_strings('de'), dict)
        # хотя бы часть строк в de должна отличаться от ru — иначе это копия
        ru = i18n.get_strings('ru')
        de = i18n.get_strings('de')
        differ = [k for k in ru if ru[k] != de[k]]
        self.assertGreater(len(differ), len(ru) // 2, 'de почти не отличается от ru')

    def test_values_are_nonempty_strings(self):
        for lang in i18n.LANGUAGES:
            strings = i18n.get_strings(lang)
            self.assertIsInstance(strings, dict)
            for key, value in strings.items():
                with self.subTest(lang=lang, key=key):
                    self.assertIsInstance(key, str)
                    self.assertTrue(key, 'пустой ключ')
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip(), 'пустое значение для %s/%s' % (lang, key))

    def test_placeholders_match_between_languages(self):
        ru = i18n.get_strings('ru')
        de = i18n.get_strings('de')
        for key in ru:
            with self.subTest(key=key):
                self.assertEqual(set(PLACEHOLDER.findall(ru[key])),
                                 set(PLACEHOLDER.findall(de.get(key, ''))),
                                 'плейсхолдеры ru/de расходятся для %s' % key)

    def test_placeholders_are_valid_format_fields(self):
        """Все {..} в строках — валидные имена для str.format (нет {0}, {}, {a.b})."""
        for lang in i18n.LANGUAGES:
            for key, value in i18n.get_strings(lang).items():
                raw = re.findall(r'\{([^{}]*)\}', value)
                for ph in raw:
                    with self.subTest(lang=lang, key=key, placeholder=ph):
                        self.assertRegex(ph, r'^[A-Za-z_]\w*$')

    def test_required_keys_present(self):
        """Ключи, на которые опираются экраны §8 и тест API (nav_today)."""
        ru = i18n.get_strings('ru')
        self.assertIn('nav_today', ru)
        nav_keys = [k for k in ru if k.startswith('nav_')]
        self.assertGreaterEqual(len(nav_keys), 5, '§8: 5 экранов навигации: %s' % nav_keys)

    def test_group_labels_present(self):
        """Подписи для всех шести групп §3 должны существовать в i18n (в каком-то виде)."""
        ru = i18n.get_strings('ru')
        joined = ' '.join(ru.keys())
        for g in ('now', 'priority', 'queue', 'delegate', 'someday', 'waiting'):
            with self.subTest(group=g):
                self.assertRegex(joined, r'(^|[ _])' + g + r'($|[ _])', 'нет ключа с подписью группы %s' % g)

    def test_get_strings_returns_copy_or_same_each_call(self):
        a = i18n.get_strings('ru')
        b = i18n.get_strings('ru')
        self.assertEqual(a, b)


if __name__ == '__main__':
    unittest.main()
