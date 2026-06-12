from mentions import strip_mention, thread_id_of


def test_strip_mention_uses_html_spark_mention():
    html = '<p><spark-mention data-object-type="person" data-object-id="X">Claude_Helpdesk</spark-mention> how do I install?</p>'
    assert strip_mention("Claude_Helpdesk how do I install?", html, "Claude_Helpdesk") == "how do I install?"


def test_strip_mention_html_is_name_agnostic():
    html = '<p><spark-mention data-object-id="Y">Totally Different Bot</spark-mention> hello there</p>'
    assert strip_mention("Totally Different Bot hello there", html, "anything") == "hello there"


def test_strip_mention_fallback_to_display_name_when_no_html():
    assert strip_mention("Claude_Helpdesk what now", "", "Claude_Helpdesk") == "what now"


def test_strip_mention_fallback_strips_first_token_if_name_mismatch():
    assert strip_mention("@SomeBot do the thing", "", "Claude_Helpdesk") == "do the thing"


def test_thread_id_of_top_level_message_uses_id():
    assert thread_id_of({"id": "MSG1"}) == "MSG1"


def test_thread_id_of_reply_uses_parent():
    assert thread_id_of({"id": "MSG2", "parentId": "ROOT1"}) == "ROOT1"
