#include <unity.h>
#include <math.h>
#include <string.h>
#include <string>
#include "plane_model.h"
#include "sky_model.h"
using namespace planes;
static Reply R;

static const char *SAMPLE =
  "{\"msg\":\"No error\",\"now\":1795656991501,\"total\":3,\"ac\":["
  "{\"hex\":\"a66239\",\"flight\":\"JBU2687 \",\"t\":\"A320\",\"alt_baro\":\"ground\",\"gs\":0.0,\"lat\":41.72477,\"lon\":-71.435556},"
  "{\"hex\":\"ad4afe\",\"flight\":\"AAL3102 \",\"t\":\"B738\",\"alt_baro\":12000,\"gs\":450.0,\"track\":90.0,\"lat\":42.0,\"lon\":-71.3},"
  "{\"hex\":\"abc123\",\"alt_baro\":3000,\"lat\":41.95,\"lon\":-71.3}]}";

void setUp() {}
void tearDown() {}

static void test_parse_sample() {
    TEST_ASSERT_TRUE(parse(SAMPLE, strlen(SAMPLE), 41.9, -71.3, R));
    TEST_ASSERT_EQUAL(3, R.n); TEST_ASSERT_EQUAL(3, R.total);
    TEST_ASSERT_EQUAL_STRING("abc123", R.ac[0].hex);                    // nearest first (3 nm)
    TEST_ASSERT_EQUAL_STRING("", R.ac[0].flight);
    TEST_ASSERT_TRUE(isnan(R.ac[0].gs_kt));
    TEST_ASSERT_EQUAL_STRING("AAL3102", R.ac[1].flight);                // trailing space cleaned
    TEST_ASSERT_EQUAL(12000, R.ac[1].alt_ft);
    TEST_ASSERT_EQUAL(INT32_MIN, R.ac[2].alt_ft);                       // ground
    TEST_ASSERT_FLOAT_WITHIN(0.1f, 6.0f, R.ac[1].dist_nm);
}

static void test_parse_limits() {
    std::string j = "{\"now\":1,\"ac\":[";
    for (int i = 0; i < 80; ++i) {
        char b[96]; snprintf(b, sizeof(b), "%s{\"hex\":\"%06x\",\"lat\":%f,\"lon\":-71.3}", i ? "," : "", i, 41.9 + (80 - i) * 0.001);
        j += b;
    }
    j += "]}";
    TEST_ASSERT_TRUE(parse(j.c_str(), j.size(), 41.9, -71.3, R));
    TEST_ASSERT_EQUAL(60, R.n); TEST_ASSERT_EQUAL(80, R.total);
    TEST_ASSERT_EQUAL_STRING("00004f", R.ac[0].hex);                     // i = 79 is nearest
    TEST_ASSERT_FALSE(parse("nope", 4, 0, 0, R));
    std::string big(MAX_REPLY + 1, ' ');
    TEST_ASSERT_FALSE(parse(big.c_str(), big.size(), 0, 0, R));
}

static void test_dead_reckon_and_project() {
    Aircraft a{}; a.lat = 0; a.lon = 0; a.gs_kt = 480; a.track_deg = 90;
    double la, lo; dead_reckon(a, 60, la, lo);
    TEST_ASSERT_FLOAT_WITHIN(0.001, 0.1333, lo); TEST_ASSERT_FLOAT_WITHIN(0.0001, 0.0, la);
    a.gs_kt = NAN; dead_reckon(a, 60, la, lo); TEST_ASSERT_EQUAL_FLOAT(0, (float)lo);
    int x, y;
    TEST_ASSERT_TRUE(project(41.9, -71.3, 41.9 + 12.5 / 60.0, -71.3, 25, 200, x, y));
    TEST_ASSERT_INT_WITHIN(1, 0, x); TEST_ASSERT_INT_WITHIN(1, -100, y);
    TEST_ASSERT_FALSE(project(41.9, -71.3, 41.9 + 26 / 60.0, -71.3, 25, 200, x, y));
    x = 0; y = -100; sky::rotate(x, y, 90);
    TEST_ASSERT_INT_WITHIN(1, -100, x); TEST_ASSERT_INT_WITHIN(1, 0, y);
}

static void test_readout_and_route() {
    char a[48], b[48];
    TEST_ASSERT_TRUE(parse(SAMPLE, strlen(SAMPLE), 41.9, -71.3, R));
    readout(a, sizeof(a), b, sizeof(b), R.ac[1], U_NM_FT);
    TEST_ASSERT_EQUAL_STRING("AAL3102  B738", a);
    TEST_ASSERT_EQUAL_STRING("12,000 ft  -  450 kt", b);
    readout(a, sizeof(a), b, sizeof(b), R.ac[1], U_MI_FT);
    TEST_ASSERT_EQUAL_STRING("12,000 ft  -  518 mph", b);
    readout(a, sizeof(a), b, sizeof(b), R.ac[1], U_KM_M);
    TEST_ASSERT_EQUAL_STRING("3,658 m  -  833 km/h", b);
    readout(a, sizeof(a), b, sizeof(b), R.ac[2], U_NM_FT);
    TEST_ASSERT_EQUAL_STRING("on the ground", b);
    readout(a, sizeof(a), b, sizeof(b), R.ac[0], U_NM_FT);
    TEST_ASSERT_EQUAL_STRING("abc123", a);                                // no callsign: hex
    const char *rt = "{\"response\":{\"flightroute\":{\"callsign\":\"JBU1234\",\"origin\":{\"iata_code\":\"SJU\",\"icao_code\":\"TJSJ\"},"
                     "\"destination\":{\"iata_code\":\"MCO\",\"icao_code\":\"KMCO\"}}}}";
    char r[24];
    TEST_ASSERT_TRUE(route_text(rt, strlen(rt), r, sizeof(r)));
    TEST_ASSERT_EQUAL_STRING("SJU - MCO", r);
    const char *nf = "{\"response\":\"unknown callsign\"}";
    TEST_ASSERT_FALSE(route_text(nf, strlen(nf), r, sizeof(r)));
}

static void test_budget() {
    const int64_t day = 1795651200000LL;            // 2026-11-25 00:00:00 UTC (divisible by 86400000)
    Budget b;
    TEST_ASSERT_EQUAL(0, day % 86400000LL);
    const int64_t noon = day + 12 * 3600000LL;
    TEST_ASSERT_INT_WITHIN(2, 86, b.wait_s(noon));   // 43200 s left / 500 requests left
    for (int i = 0; i < 499; ++i) b.spent(noon);
    TEST_ASSERT_INT_WITHIN(2, 43200, b.wait_s(noon));
    b.spent(noon);
    TEST_ASSERT_EQUAL(-1, b.wait_s(noon));
    TEST_ASSERT_TRUE(b.wait_s(day + 86400000LL) >= 30);  // new day
    Budget c;
    c.limited(noon, 120);
    TEST_ASSERT_TRUE(c.wait_s(noon) >= 120);
    Budget d; d.perDay = 7200;
    TEST_ASSERT_EQUAL(30, d.wait_s(noon));           // never under 30 s
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_parse_sample);
    RUN_TEST(test_parse_limits);
    RUN_TEST(test_dead_reckon_and_project);
    RUN_TEST(test_readout_and_route);
    RUN_TEST(test_budget);
    return UNITY_END();
}
