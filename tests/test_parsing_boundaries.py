"""Deterministic parser boundaries; no network or private records are used."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.parsing import AccessIssue, Candidate, Listing, access_check, extract_detail, extract_listing


HOST = 'fixture.xidian.edu.cn'
ORIGIN = 'https://' + HOST


class ListingBoundaryTests(unittest.TestCase):
    def listing(self, content, path='/tz.htm', source=None, **kwargs):
        return extract_listing(content, ORIGIN + path, {HOST}, source or {}, **kwargs)

    def test_div_entries_do_not_share_dates(self):
        listing = self.listing('''<title>学院</title><div class="list">
          <div><a href="/info/1/1.htm">旧的报名通知</a><span>2020-01-01</span></div>
          <div><a href="/info/1/2.htm">新的报名通知</a><span>2026-10-25</span></div>
        </div>''')
        self.assertEqual([item.date for item in listing.candidates], ['2020-01-01', '2026-10-25'])

    def test_shared_container_date_is_not_owned_by_either_entry(self):
        listing = self.listing('''<div><span>2020-01-01</span>
          <div><a href="/info/1/1.htm">第一报名通知</a></div>
          <div><a href="/info/1/2.htm">第二报名通知</a></div></div>''')
        self.assertEqual([item.date for item in listing.candidates], [None, None])

    def test_same_identity_image_and_text_links_share_entry(self):
        listing = self.listing('''<div><div>
          <div><a href="/info/1/2.htm" title="关于报名的通知"><img alt="配图"></a></div>
          <div><a href="/content.jsp?wbnewsid=2">关于报名的通知</a></div>
          <time datetime="2026-10-25T09:30:00+08:00">10月25日</time>
        </div><div><a href="/info/1/3.htm">另一个报名通知</a><span>2020-01-01</span></div></div>''')
        self.assertEqual(len(listing.candidates), 2)
        self.assertEqual(listing.candidates[0].date, '2026-10-25')

    def test_title_and_prose_activity_dates_are_not_publication_dates(self):
        for content in (
            '<div><a href="/info/1/2.htm">关于2026年10月25日活动的通知</a></div>',
            '<div><a href="/info/1/2.htm">关于报名的通知</a><p>活动将在2026-10-25举行。</p></div>',
        ):
            with self.subTest(content=content):
                self.assertIsNone(self.listing(content).candidates[0].date)

    def test_multiple_valid_dates_are_ambiguous(self):
        listing = self.listing('''<div><a href="/info/1/2.htm">关于报名的通知</a>
          <span>2026-10-24</span><span>2026-10-25</span></div>''')
        self.assertIsNone(listing.candidates[0].date)

    def test_invalid_date_is_ignored_and_plain_date_text_is_supported(self):
        listing = self.listing('''<li><a href="/info/1/2.htm">关于报名的通知</a>
          <span>2026-02-31</span>2026-10-25</li>''')
        self.assertEqual(listing.candidates[0].date, '2026-10-25')

    def test_custom_detail_links_also_bound_dates(self):
        listing = self.listing('''<div><span>2020-01-01</span>
          <div><a class="entry" href="/detail?id=1">第一报名通知</a></div>
          <div><a class="entry" href="/detail?id=2">第二报名通知</a></div></div>''',
            source={'detail_link_selector': 'a.entry'})
        self.assertEqual([item.date for item in listing.candidates], [None, None])

    def test_only_explicit_next_page_receives_caller_trust(self):
        first = self.listing('''<title>学院</title>
          <a href="/tz/2.htm">下一页</a><a href="/other.htm">通知公告</a>''')
        self.assertTrue(first.trusted)
        self.assertEqual(first.next_pages, [ORIGIN + '/tz/2.htm'])
        self.assertEqual(first.sections, [ORIGIN + '/other.htm'])
        content = '''<title>学院</title>
          <a href="/info/1/2.htm">国家助学金申请安排</a>
          <a href="/info/1/3.htm">学院成功举办交流活动</a>
          <a href="https://outside.example/info/1/4.htm">重要通知</a>'''
        second = self.listing(content, '/tz/2.htm', trusted_section=first.trusted)
        self.assertTrue(second.trusted)
        self.assertEqual([item.title for item in second.candidates], ['国家助学金申请安排'])
        self.assertTrue(second.candidates[0].trusted)
        self.assertEqual(self.listing(content, '/other.htm').candidates, [])

    def test_split_calendar_date_inside_link_is_entry_metadata(self):
        listing = self.listing('''<ul><li><a href="/info/1/1.htm" title="关于报名的通知">
          <div class="date1"><p>30</p><p>2026-09</p></div>
          <div class="text"><p class="title">关于2027年10月25日活动的通知</p></div>
        </a></li><li><a href="/info/1/2.htm" title="另一报名通知">
          <div class="date"><span>2026-10-20</span></div>
        </a></li></ul>''')
        self.assertEqual([item.date for item in listing.candidates], ['2026-09-30', '2026-10-20'])

    def test_rel_and_accessible_next_links_without_visible_text(self):
        for markup in ('<a href="/archive?p=2" rel="next"><img alt="下一页"></a>',
                       '<a href="/archive?p=2" aria-label="下一页"><i></i></a>'):
            with self.subTest(markup=markup):
                self.assertEqual(self.listing(markup).next_pages, [ORIGIN + '/archive?p=2'])
        self.assertEqual(self.listing('<a href="https://outside.example/list" rel="next">Next</a>').next_pages, [])

    def test_nonstandard_article_paths_and_binary_exclusion(self):
        listing = self.listing('''<title>通知公告</title><ul>
          <li><a href="/announcement/scholarship">关于奖学金申请的通知</a><time>2026-10-25</time></li>
          <li><a href="/notice/exam.html">考试时间安排</a><time>2026-10-24</time></li>
          <li><a href="/download/application.pdf">关于申请的通知</a></li>
        </ul><nav><a href="/notice/archive">更多通知公告</a></nav>''')
        self.assertEqual([item.url for item in listing.candidates],
                         [ORIGIN + '/announcement/scholarship', ORIGIN + '/notice/exam.html'])

    def test_explicit_custom_dates_and_pagination_are_scoped(self):
        listing = self.listing('''<div class="rows">
          <div><a class="entry" href="/document?id=1">考试安排</a><span class="published">日期：2026-10-25</span></div>
          <div><a class="entry" href="/document?id=2">申请安排</a><span class="published">日期：2026-10-24</span></div>
        </div><a class="forward" href="/archive?offset=2">继续查看</a>''', '/archive',
            {'list_selector': '.rows', 'detail_link_selector': 'a.entry',
             'list_date_selector': '.published', 'next_page_selector': 'a.forward'})
        self.assertEqual([item.date for item in listing.candidates], ['2026-10-25', '2026-10-24'])
        self.assertEqual(listing.next_pages, [ORIGIN + '/archive?offset=2'])

    def test_shared_navigation_is_excluded_without_hiding_main_banner_content(self):
        listing = self.listing('''<div class="top"><ul><li>
          <a href="/info/1/9.htm">书院风采</a></li></ul></div>
          <div class="nav-banner"><ul><li><a href="/info/1/1.htm" title="关于材料提交的通知">
          <p>关于材料提交的通知</p><b>2026-10-25</b></a></li></ul></div>
          <ul><li><a href="/calendar.htm">教学日历</a></li></ul>''')
        self.assertEqual([(item.url, item.date) for item in listing.candidates],
                         [(ORIGIN+'/info/1/1.htm', '2026-10-25')])

    def test_trust_survives_missing_selector_and_listing_positional_contract(self):
        listing = self.listing('<title>学院</title>', '/list', {'list_selector': '.missing'},
                               trusted_section=True)
        self.assertTrue(listing.trusted)
        self.assertEqual(listing.candidates, [])
        self.assertFalse(Listing('学院', [], [], []).trusted)


class DetailDateBoundaryTests(unittest.TestCase):
    def detail(self, content, listed_date=None):
        candidate = Candidate(ORIGIN + '/info/1/2.htm', '关于报名的通知', listed_date,
                              ORIGIN + '/tz.htm', True)
        return extract_detail(content, candidate.url, candidate, {})

    def test_meta_publication_date_wins_over_sidebar_and_listing(self):
        detail = self.detail('''<meta name="PubDate" content="2026-10-25">
          <aside>发布时间：2020-01-01</aside><article><h1>关于报名的通知</h1>
          <div class="v_news_content">报名详情</div></article>''', '2026-10-24')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_fallback_is_labeled_and_scoped_to_article(self):
        detail = self.detail('''<aside>发布时间：2020-01-01</aside>
          <div class="article"><h1>关于报名的通知</h1>
          <div class="metadata">来源：学院 发布时间：<span>2026年10月25日</span></div>
          <div class="v_news_content">报名活动在2026-11-01举行。</div></div>''')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_sidebar_inside_article_container_is_excluded(self):
        detail = self.detail('''<div><h1>关于报名的通知</h1>
          <div class="sidebar">发布时间：2020-01-01</div>
          <div class="metadata">发布日期：2026-10-25</div>
          <div class="v_news_content">报名详情</div></div>''')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_document_wide_sidebar_is_not_fallback(self):
        content = '''<html><body><aside>发布时间：2020-01-01</aside>
          <h1>关于报名的通知</h1><div class="v_news_content">报名详情</div></body></html>'''
        self.assertIsNone(self.detail(content)['published_date'])
        self.assertEqual(self.detail(content, '2026-10-25')['published_date'], '2026-10-25')

    def test_body_activity_date_and_unlabeled_date_do_not_override_listing(self):
        detail = self.detail('''<article><h1>关于报名的通知</h1><span>2020-01-01</span>
          <span>活动日期：2026-11-01</span>
          <div class="v_news_content">活动日期：2026-11-01</div></article>''', '2026-10-25')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_ambiguous_article_metadata_does_not_choose_first_date(self):
        detail = self.detail('''<article><h1>关于报名的通知</h1>
          <span>发布时间：2026-10-24</span><span>发布日期：2026-10-25</span>
          <div class="v_news_content">报名详情</div></article>''')
        self.assertIsNone(detail['published_date'])

    def test_article_element_as_body_can_supply_own_metadata(self):
        detail = self.detail('''<article><h1>关于报名的通知</h1>
          <div>发布日期：2026-10-25</div><p>报名详情</p></article>''')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_trusted_listing_cannot_hide_news_detail_title(self):
        with self.assertRaises(ValueError):
            self.detail('''<article><h1>学院成功举办交流活动</h1>
              <div class="v_news_content">活动纪实</div></article>''')

    def test_article_title_meta_does_not_replace_visible_heading(self):
        detail = self.detail('''<html><head>
          <meta name="ArticleTitle" content="关于报名的通知"></head><body>
          <div class="article"><h1>关于报名的通知</h1>
          <p>发布时间：2014-03-05</p><div class="v_news_content">报名详情</div></div></body></html>''')
        self.assertEqual(detail['title'], '关于报名的通知')
        self.assertEqual(detail['published_date'], '2014-03-05')

    def test_article_title_meta_is_a_title_fallback(self):
        detail = self.detail('''<html><head>
          <meta name="ArticleTitle" content="关于举办数学建模竞赛的通知"></head>
          <body><div class="v_news_content">竞赛详情</div></body></html>''')
        self.assertEqual(detail['title'], '关于举办数学建模竞赛的通知')

    def test_section_heading_is_not_the_article_title(self):
        detail = self.detail('''<div class="position"><h2 class="title">通知公告</h2></div>
          <div class="main"><div class="title"><h4>关于举办数学建模竞赛的通知</h4></div>
          <p>发布时间：2026-10-01</p><div class="v_news_content">竞赛详情</div></div>''')
        self.assertEqual(detail['title'], '关于举办数学建模竞赛的通知')
        self.assertEqual(detail['published_date'], '2026-10-01')

    def test_site_header_heading_does_not_mask_article_title(self):
        detail = self.detail('''<div class="header"><h1>西安电子科技大学</h1></div>
          <div class="main"><div class="title"><h4>关于举办数学建模竞赛的通知</h4></div>
          <p>发布时间：2026-10-01</p><div class="v_news_content">竞赛详情</div></div>''')
        self.assertEqual(detail['title'], '关于举办数学建模竞赛的通知')
        self.assertEqual(detail['published_date'], '2026-10-01')

    def test_untitled_detail_does_not_borrow_sidebar_date(self):
        detail = self.detail('''<div class="main"><div class="side"><span>发布日期：2013-05-01</span></div>
          <div class="v_news_content">报名详情</div></div>''', '2026-10-20')
        self.assertEqual(detail['published_date'], '2026-10-20')

    def test_body_subheading_does_not_override_formal_title(self):
        detail = self.detail('''<div class="article"><h2 class="title">关于报名的通知</h2>
          <p>发布时间：2026-10-25</p>
          <div class="v_news_content"><h1>报名须知</h1><p>报名详情</p></div></div>''')
        self.assertEqual(detail['title'], '关于报名的通知')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_body_subheading_news_title_does_not_exclude_notice(self):
        detail = self.detail('''<div class="article"><h2 class="title">关于报名的通知</h2>
          <div class="v_news_content"><h1>学院成功举办交流活动</h1><p>报名详情</p></div></div>''')
        self.assertEqual(detail['title'], '关于报名的通知')

    def test_event_date_in_title_is_not_publication_date(self):
        detail = self.detail('''<div class="article">
          <h2 class="title">关于报名的通知（日期：2026-11-01）</h2>
          <div class="v_news_content">报名详情</div></div>''', '2026-10-25')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_title_text_does_not_hide_nested_publication_metadata(self):
        detail = self.detail('''<div class="article">
          <h2 class="title">关于报名的通知（日期：2026-11-01）
          <p class="info">发布时间：2026-10-25</p></h2>
          <div class="v_news_content">报名详情</div></div>''', '2026-10-20')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_enclosing_article_keeps_own_heading_and_metadata(self):
        detail = self.detail('''<header><h1>西安电子科技大学</h1></header>
          <article><h1>关于报名的通知</h1>
          <div>发布时间：2026-10-25</div><p>请提交报名材料。</p></article>''')
        self.assertEqual(detail['title'], '关于报名的通知')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_site_header_news_heading_does_not_exclude_enclosed_notice(self):
        detail = self.detail('''<div class="header"><h1>学院成功举办周年庆典</h1></div>
          <article><h1>关于报名的通知</h1>
          <div>发布时间：2026-10-25</div><p>请提交报名材料。</p></article>''')
        self.assertEqual(detail['title'], '关于报名的通知')

    def test_heading_wrapper_does_not_reintroduce_event_date(self):
        detail = self.detail('''<div class="article"><div class="heading">
          <h1>关于报名的通知（日期：2026-11-01）</h1></div>
          <div class="v_news_content">报名详情</div></div>''', '2026-10-25')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_article_header_heading_beats_breadcrumb_heading(self):
        detail = self.detail('''<div class="position"><h1>当前位置：首页 > 通知公告</h1></div>
          <article><header><h1>关于报名的通知</h1></header>
          <p>发布时间：2026-10-25</p><div class="v_news_content">报名详情</div></article>''')
        self.assertEqual(detail['title'], '关于报名的通知')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_event_date_in_article_header_title_is_not_publication_date(self):
        detail = self.detail('''<div class="wrap"><div class="head"><h1>西安电子科技大学</h1></div>
          <div class="article"><div class="header"><h1>关于报名的通知（日期：2026-11-01）</h1></div>
          <div class="v_news_content">报名详情</div></div></div>''', '2026-10-20')
        self.assertEqual(detail['published_date'], '2026-10-20')

    def test_content_id_inside_article_is_body_text(self):
        detail = self.detail('''<div class="article"><h2 class="title">关于报名的通知</h2>
          <p>发布时间：2026-10-25</p>
          <div id="content"><h1>报名须知</h1><p>报名详情</p></div></div>''')
        self.assertEqual(detail['title'], '关于报名的通知')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_listing_title_breaks_the_tie_between_headings(self):
        detail = self.detail('''<div class="article"><h1>当前位置：首页 &gt; 通知公告</h1>
          <div class="head"><h2 class="title">关于报名的通知</h2></div>
          <p>发布时间：2026-10-25</p><div class="v_news_content">报名详情</div></div>''')
        self.assertEqual(detail['title'], '关于报名的通知')
        self.assertEqual(detail['published_date'], '2026-10-25')

    def test_newsy_kicker_does_not_hide_the_notice_title(self):
        detail = self.detail('''<div class="article"><div class="kicker">
          <h1>学院成功举办交流活动</h1></div>
          <h2 class="title">关于报名的通知</h2>
          <div class="v_news_content">报名详情</div></div>''')
        self.assertEqual(detail['title'], '关于报名的通知')

    def test_detail_heading_wins_over_a_different_listing_title(self):
        detail = self.detail('''<div class="article"><h2 class="title">关于举办数学建模竞赛的通知（第二轮）</h2>
          <p>发布时间：2026-10-25</p><div class="v_news_content">竞赛详情</div></div>''',
                             '2026-10-20')
        self.assertEqual(detail['title'], '关于举办数学建模竞赛的通知（第二轮）')


class AccessCodeTests(unittest.TestCase):
    def test_access_classification_exposes_stable_codes(self):
        cases = (
            ('<title>统一身份认证平台</title>', 'login_required'),
            ('<input type="password">', 'login_required'),
            ('<title>系统提示</title>校外访问请使用校园网或aTrust', 'campus_network_required'),
            ('<title>Just a moment</title>', 'verification_required'),
            ('<title>安全验证</title>', 'verification_required'),
            ('<title>Access Denied</title>', 'access_denied'),
            ('<title>系统提示</title>', 'access_denied'),
        )
        for content, code in cases:
            with self.subTest(code=code, content=content):
                with self.assertRaises(AccessIssue) as raised:
                    access_check(content, ORIGIN + '/')
                self.assertEqual(raised.exception.code, code)

    def test_access_error_preserves_message_and_default_type_contract(self):
        error = AccessIssue('访问受限')
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), '访问受限')
        self.assertEqual(error.code, 'access_denied')


if __name__ == '__main__':
    unittest.main()
