"""TestSummarizeToolResultWebExtract behavior contracts."""

import json
from agent.context_compressor import _summarize_tool_result


class TestSummarizeToolResultWebExtract:
    """Pre-compression pruning must survive web_extract calls whose ``urls`` are
    web_search result dicts ({"url"/"href": ...}), which models routinely forward
    straight into web_extract.
    """

    CONTENT = "x" * 500  # >200 chars so the pruning pass actually summarizes


    def test_single_dict_url_is_unwrapped_not_stringified(self):
        args = json.dumps({"urls": [{"url": "https://example.com/a", "title": "A"}]})
        summary = _summarize_tool_result("web_extract", args, self.CONTENT)
        assert summary == "[web_extract] https://example.com/a (500 chars)"
        assert "{" not in summary  # no raw dict repr leaked into the summary

    def test_href_key_is_unwrapped(self):
        args = json.dumps({"urls": [{"href": "https://example.com/h"}]})
        summary = _summarize_tool_result("web_extract", args, self.CONTENT)
        assert summary == "[web_extract] https://example.com/h (500 chars)"
