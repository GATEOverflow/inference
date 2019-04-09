#ifndef QUERY_SAMPLE_H_
#define QUERY_SAMPLE_H_

// Defines the structs involved in issuing a query and responding to a query.
// These are broken out into their own files since they are exposed as part
// of the C API and we want to avoid C clients including C++ code.

namespace mlperf {

// QueryId represents a unique identifier for an issued query.
typedef intptr_t QueryId;

// QuerySample represents the smallest unit of input inference can run on.
// A query will consist of one or more samples.
typedef uint64_t QuerySample;

// QuerySampleResponse represents a single response to QuerySample
struct QuerySampleResponse {
  intptr_t data;
  size_t size;  // Size in bytes.
};

}  // namespace mlperf

#endif  // QUERY_SAMPLE_H_
