/*
 * Anime4K Restore-CNN runner for RKNN / rk3566.
 *
 * usage: anime4k_rknn <model.rknn> <in.rgb> <out.rgb> [W] [H] [MODE] [MODE2]
 *   in.rgb   W*H*3    uint8 RGB
 *   out.rgb  2W*2H*3  uint8 RGB（模型输出 float 0~1，这里乘 255）
 *
 * MODE:  normal | loop | perf
 *   loop 从 stdin 反复读 W*H*3，往 stdout 写 2W*2H*3，直到 EOF（模型只加载一次）
 *   perf 打印逐层耗时
 *
 * 模型配置为 mean=0 / std=255，即 uint8 0-255 归一化到 0-1，输出也是 0-1。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "rknn_api.h"

static unsigned char *read_file(const char *path, size_t *size)
{
	FILE *f = fopen(path, "rb");
	unsigned char *buf;
	long len;

	if (!f) {
		perror(path);
		return NULL;
	}
	fseek(f, 0, SEEK_END);
	len = ftell(f);
	fseek(f, 0, SEEK_SET);
	buf = malloc(len);
	if (!buf || fread(buf, 1, len, f) != (size_t)len) {
		fclose(f);
		free(buf);
		return NULL;
	}
	fclose(f);
	if (size)
		*size = (size_t)len;
	return buf;
}

static int convert_out(rknn_context ctx, rknn_output *outputs, int oh, int ow, int oc,
		       int fmt_nchw, unsigned char *dst)
{
	float *f = (float *)outputs[0].buf;
	int y, x, c;

	for (y = 0; y < oh; y++)
		for (x = 0; x < ow; x++)
			for (c = 0; c < 3; c++) {
				float v = fmt_nchw ? f[(c * oh + y) * ow + x]
						   : f[(y * ow + x) * oc + c];
				int val = (int)(v * 255.0f + 0.5f);

				if (val < 0) val = 0;
				if (val > 255) val = 255;
				dst[(y * ow + x) * 3 + c] = (unsigned char)val;
			}
	return 0;
}

int main(int argc, char **argv)
{
	rknn_context ctx = 0;
	int ret, W, H, C, ow, oh, oc, nchw_in, nchw_out;
	int loop = 0, perf = 0;
	const char *mode = argc > 6 ? argv[6] : "";
	unsigned char *model, *inbuf = NULL, *imgb = NULL, *outb = NULL;
	size_t msize = 0;
	rknn_input_output_num io_num;
	rknn_tensor_attr in_attr, out_attr;
	rknn_input inputs[1];
	rknn_output outputs[1];

	if (argc < 4) {
		fprintf(stderr, "usage: %s model.rknn in.rgb out.rgb [W H] [normal|loop|perf]\n", argv[0]);
		return 1;
	}
	if (!strcmp(mode, "loop")) loop = 1;
	if (!strcmp(mode, "perf")) perf = 1;

	model = read_file(argv[1], &msize);
	if (!model) return 1;

	ret = rknn_init(&ctx, model, (uint32_t)msize,
			perf ? RKNN_FLAG_COLLECT_PERF_MASK : 0, NULL);
	if (ret < 0) {
		fprintf(stderr, "rknn_init failed: %d\n", ret);
		return 1;
	}

	memset(&io_num, 0, sizeof(io_num));
	rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
	memset(&in_attr, 0, sizeof(in_attr));
	in_attr.index = 0;
	rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &in_attr, sizeof(in_attr));
	if (in_attr.fmt == RKNN_TENSOR_NCHW) {
		C = in_attr.dims[1]; H = in_attr.dims[2]; W = in_attr.dims[3]; nchw_in = 1;
	} else {
		H = in_attr.dims[1]; W = in_attr.dims[2]; C = in_attr.dims[3]; nchw_in = 0;
	}
	memset(&out_attr, 0, sizeof(out_attr));
	out_attr.index = 0;
	rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &out_attr, sizeof(out_attr));
	if (out_attr.fmt == RKNN_TENSOR_NCHW) {
		oc = out_attr.dims[1]; oh = out_attr.dims[2]; ow = out_attr.dims[3]; nchw_out = 1;
	} else {
		oh = out_attr.dims[1]; ow = out_attr.dims[2]; oc = out_attr.dims[3]; nchw_out = 0;
	}
	if (argc > 5) { W = atoi(argv[4]); H = atoi(argv[5]); }
	fprintf(stderr, "in %dx%d ch=%d (%s)  out %dx%d ch=%d\n", W, H, C,
		nchw_in ? "nchw" : "nhwc", ow, oh, oc);

	if (C != 3) {
		fprintf(stderr, "expected 3 input channels, model has %d\n", C);
		return 1;
	}

	inbuf = malloc((size_t)W * H * 3);
	outb = malloc((size_t)ow * oh * 3);
	imgb = malloc((size_t)W * H * 3);
	if (!inbuf || !outb || !imgb) return 1;

	memset(inputs, 0, sizeof(inputs));
	inputs[0].index = 0;
	inputs[0].type = RKNN_TENSOR_UINT8;
	inputs[0].size = (uint32_t)(W * H * 3);
	inputs[0].fmt = RKNN_TENSOR_NHWC;
	inputs[0].pass_through = 0;

	if (loop) {
		inputs[0].buf = inbuf;
		while (fread(imgb, 1, (size_t)W * H * 3, stdin) == (size_t)W * H * 3) {
			memcpy(inbuf, imgb, (size_t)W * H * 3);
			if (rknn_inputs_set(ctx, 1, inputs) < 0) break;
			if (rknn_run(ctx, NULL) < 0) break;
			memset(outputs, 0, sizeof(outputs));
			outputs[0].index = 0;
			outputs[0].want_float = 1;
			if (rknn_outputs_get(ctx, 1, outputs, NULL) < 0) break;
			convert_out(ctx, outputs, oh, ow, oc, nchw_out, outb);
			rknn_outputs_release(ctx, 1, outputs);
			fwrite(outb, 1, (size_t)ow * oh * 3, stdout);
			fflush(stdout);
		}
		return 0;
	}

	imgb = read_file(argv[2], NULL);
	if (!imgb) return 1;
	memcpy(inbuf, imgb, (size_t)W * H * 3);
	inputs[0].buf = inbuf;
	if (rknn_inputs_set(ctx, 1, inputs) < 0) return 1;
	if (rknn_run(ctx, NULL) < 0) return 1;
	memset(outputs, 0, sizeof(outputs));
	outputs[0].index = 0;
	outputs[0].want_float = 1;
	if (rknn_outputs_get(ctx, 1, outputs, NULL) < 0) return 1;
	convert_out(ctx, outputs, oh, ow, oc, nchw_out, outb);
	rknn_outputs_release(ctx, 1, outputs);

	{
		FILE *fo = fopen(argv[3], "wb");

		if (!fo) return 1;
		fwrite(outb, 1, (size_t)ow * oh * 3, fo);
		fclose(fo);
	}

	if (perf) {
		rknn_perf_detail pd;
		rknn_perf_run pr;

		memset(&pd, 0, sizeof(pd));
		if (rknn_query(ctx, RKNN_QUERY_PERF_DETAIL, &pd, sizeof(pd)) == 0 && pd.perf_data)
			fwrite(pd.perf_data, 1, (size_t)pd.data_len, stderr);
		memset(&pr, 0, sizeof(pr));
		if (rknn_query(ctx, RKNN_QUERY_PERF_RUN, &pr, sizeof(pr)) == 0)
			fprintf(stderr, "\nTOTAL_RUN_US %lld\n", (long long)pr.run_duration);
	}
	return 0;
}
