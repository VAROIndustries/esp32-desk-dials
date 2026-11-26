#include <unity.h>
#include <string.h>
#include <string>
#include "sky_model.h"

using namespace sky;
static Reply R;   // ~60 KB: static, not on the stack

static const char *LIVE =
    "{\"t\":1795651652,\"poll\":50,\"lag_ms\":782,\"screens\":{"
    "\"starlink\":{\"count\":14,\"age_h\":2.0,\"objects\":[{\"n\":\"STARLINK-30482\",\"tag\":\"\",\"trk\":"
    "[[45.3,78.7],[54.6,75.1],[60.2,71.5],[63.9,67.9],[66.5,64.4],[68.4,61.0],[69.9,57.8],[71.0,54.8],"
    "[72.0,51.9],[72.7,49.2],[73.4,46.7],[73.9,44.3],[74.4,42.1]]}],\"next\":null},"
    "\"nasa\":{\"count\":0,\"age_h\":0.9,\"objects\":[],\"next\":{\"n\":\"TERRA\",\"tag\":\"\",\"in_s\":2649,\"from\":\"S\",\"max_el\":74}},"
    "\"spy\":{\"locked\":true}}}";

void setUp() {}
void tearDown() {}

static void test_parses_the_live_reply() {
    TEST_ASSERT_TRUE(parse(LIVE, strlen(LIVE), R));
    TEST_ASSERT_EQUAL(50, R.poll_s);
    TEST_ASSERT_EQUAL(782, R.lag_ms);
    const Screen &s = R.scr[SCR_STARLINK];
    TEST_ASSERT_EQUAL(ST_OK, s.state);
    TEST_ASSERT_EQUAL(14, s.count);
    TEST_ASSERT_EQUAL(1, s.n);
    TEST_ASSERT_EQUAL_STRING("STARLINK-30482", s.obj[0].name);
    TEST_ASSERT_EQUAL(13, s.obj[0].npts);
    TEST_ASSERT_EQUAL_FLOAT(74.4f, s.obj[0].az[12]);
    TEST_ASSERT_EQUAL(-1, s.hobby);
    const Screen &n = R.scr[SCR_NASA];
    TEST_ASSERT_TRUE(n.next.have);
    TEST_ASSERT_EQUAL_STRING("TERRA", n.next.name);
    TEST_ASSERT_EQUAL(2649, n.next.in_s);
    TEST_ASSERT_EQUAL(ST_LOCKED, R.scr[SCR_SPY].state);
}

static void test_enforces_its_own_limits() {
    std::string j = "{\"poll\":50,\"lag_ms\":0,\"screens\":{\"spy\":{\"count\":500,\"age_h\":1,\"hobby\":true,\"objects\":[";
    for (int i = 0; i < 70; ++i) {
        if (i) j += ",";
        j += "{\"n\":\"#FF0000 VERY-LONG-NAME-THAT-GOES-ON-AND-ON\",\"tag\":\"HOBBY\",\"old\":true,\"trk\":[";
        for (int k = 0; k < 20; ++k) j += std::string(k ? "," : "") + "[10,20]";
        j += "]}";
    }
    j += "],\"next\":null},\"bogus\":{\"count\":1}}}";
    TEST_ASSERT_TRUE(parse(j.c_str(), j.size(), R));
    const Screen &s = R.scr[SCR_SPY];
    TEST_ASSERT_EQUAL(60, s.n);
    TEST_ASSERT_EQUAL(500, s.count);
    TEST_ASSERT_EQUAL(13, s.obj[59].npts);
    TEST_ASSERT_EQUAL(1, s.hobby);
    TEST_ASSERT_TRUE(s.obj[0].old);
    TEST_ASSERT_NULL(strchr(s.obj[0].name, '#'));
    TEST_ASSERT_TRUE(strlen(s.obj[0].name) <= 23);
    TEST_ASSERT_EQUAL(ST_NONE, R.scr[SCR_STARLINK].state);
}

static void test_rejects_bad_bodies() {
    TEST_ASSERT_FALSE(parse("not json", 8, R));
    TEST_ASSERT_FALSE(parse("{\"poll\":50}", 11, R));                 // no screens
    std::string big(MAX_REPLY + 1, ' ');
    TEST_ASSERT_FALSE(parse(big.c_str(), big.size(), R));
    const char *un = "{\"screens\":{\"nasa\":{\"unavailable\":true}}}";
    TEST_ASSERT_TRUE(parse(un, strlen(un), R));
    TEST_ASSERT_EQUAL(ST_UNAVAILABLE, R.scr[SCR_NASA].state);
    TEST_ASSERT_EQUAL(50, R.poll_s);                                   // missing poll -> default
}

static void test_position_interpolates_and_wraps() {
    Obj o{};
    o.npts = 3;
    o.az[0] = 350; o.el[0] = 10;
    o.az[1] = 10;  o.el[1] = 20;
    o.az[2] = 30;  o.el[2] = 30;
    float az, el;
    TEST_ASSERT_TRUE(position(o, 2.5f, az, el));
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 0.0f, az);                         // short way across north
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 15.0f, el);
    TEST_ASSERT_TRUE(position(o, 10.0f, az, el));
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 30.0f, az);
    TEST_ASSERT_FALSE(position(o, 10.1f, az, el));
    TEST_ASSERT_FALSE(position(o, -0.1f, az, el));
}

static void test_project_zenith_rim_and_compass() {
    int x, y;
    TEST_ASSERT_TRUE(project(123, 90, 20, 200, x, y));
    TEST_ASSERT_EQUAL(0, x); TEST_ASSERT_EQUAL(0, y);                 // zenith at the centre
    TEST_ASSERT_TRUE(project(0, 20, 20, 200, x, y));
    TEST_ASSERT_EQUAL(0, x); TEST_ASSERT_EQUAL(-200, y);              // north, on the rim, up
    TEST_ASSERT_TRUE(project(90, 55, 20, 200, x, y));
    TEST_ASSERT_EQUAL(100, x); TEST_ASSERT_EQUAL(0, y);               // east, half way, right
    TEST_ASSERT_FALSE(project(90, 19.9f, 20, 200, x, y));             // below the cutoff
}

static void test_query_and_next_text() {
    char q[160];
    const ScreenId ids[] = { SCR_STARLINK, SCR_NASA };
    const int cuts[SCR_COUNT] = { 45, 20, 20 };
    TEST_ASSERT_TRUE(build_query(q, sizeof(q), 41.9349f, -71.3172f, ids, 2, cuts) > 0);
    TEST_ASSERT_EQUAL_STRING("lat=41.9&lon=-71.3&screens=starlink,nasa&cut=starlink:45", q);
    TEST_ASSERT_EQUAL(-1, build_query(q, 10, 41.9f, -71.3f, ids, 2, cuts));
    char t[96];
    Next nx{true, "TERRA", 2649, "S", 74};
    next_text(t, sizeof(t), nx);
    TEST_ASSERT_EQUAL_STRING("TERRA in 44 min, from S, peaks 74 deg up", t);
    nx.in_s = 3 * 3600 + 600;
    next_text(t, sizeof(t), nx);
    TEST_ASSERT_EQUAL_STRING("TERRA in 3 h 10 min, from S, peaks 74 deg up", t);
}

static void test_clean_text() {
    char b[NAME_LEN];
    clean_text(b, sizeof(b), "  #F00 ISS (ZARYA)\n<x>  ");
    TEST_ASSERT_EQUAL_STRING("F00 ISS (ZARYA)x", b);
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_parses_the_live_reply);
    RUN_TEST(test_enforces_its_own_limits);
    RUN_TEST(test_rejects_bad_bodies);
    RUN_TEST(test_position_interpolates_and_wraps);
    RUN_TEST(test_project_zenith_rim_and_compass);
    RUN_TEST(test_query_and_next_text);
    RUN_TEST(test_clean_text);
    return UNITY_END();
}
