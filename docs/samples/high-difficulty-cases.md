# High-Difficulty Regression Samples

This file tracks real-world URLs that are useful as manual regression samples when tuning stability and efficiency.

These are not CI fixtures. They are external-network cases that can change over time, disappear, or become restricted.

## Sample 1: Long X/Twitter HLS Video

- Platform: X/Twitter
- URL:
  `https://x.com/dachaoren/status/2044462598049927589?s=12&t=SZy8fljX4_VNNZsrB1hBxQ`
- Observed output:
  `太阳闯关记 - 腾讯的技术大神亲自教学  37分钟手把手！ 0基础用AI编程 开发同城跑腿小程序+APP  教程录制非常仔细 一看就懂。一次搞定 [2044254648916226048].mp4`
- Observed final size:
  `193532293 bytes`

### Why It Is Difficult

- The post resolves to a long HLS-style download rather than a small simple clip
- The downloader must fetch many video fragments and a separate audio stream
- The job takes meaningfully longer than a typical short social video
- A false timeout or premature failure classification would incorrectly treat a healthy long-running download as broken

### Expected Behavior

- The download may take several minutes
- Video and audio should both complete successfully
- The final output should be a single playable `.mp4`
- The final output should remain QuickTime / PowerPoint compatible without requiring a second manual fix

### What To Watch During Manual Regression

- The process should keep making forward progress instead of stalling permanently
- The downloader should not misclassify the job as `network_unstable` while fragments are still growing
- The final summary should report success rather than partial-video or audio-only output
- Temporary `.part` files should eventually collapse into a final `.mp4`
