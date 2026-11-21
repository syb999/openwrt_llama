/*
 * MI-GAN (4ch inpainting) runner for RKNN / rk3566.
 *
 * usage: migan_rknn <model.rknn> <img.rgb> <mask.gray> <out.rgb>
 *
 *   img.rgb   W*H*3  uint8, RGB, hole pixels already zeroed (image * (1-mask))
 *   mask.gray W*H    uint8, 255 = keep, 0 = hole
 *   out.rgb   W*H*3  uint8, network output ((y*127.5)+127.5)
 *
 * The network input is 4 channels: [0.5-mask, R, G, B].  We feed channel 0 as
 * 255*(1-mask) and rely on the model's mean/std (127.5 / 255.0) to turn that
 * into 0.5-mask.
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
	if (!buf) {
		fclose(f);
		return NULL;
	}
	if (fread(buf, 1, len, f) != (size_t)len) {
		free(buf);
		fclose(f);
		return NULL;
	}
	fclose(f);
	*size = (size_t)len;
	return buf;
}

int main(int argc, char **argv)
{
	rknn_context ctx = 0;
	int ret;
	unsigned char *model = NULL, *img = NULL, *mask = NULL;
	size_t model_size = 0;
	rknn_input_output_num io_num;
	rknn_tensor_attr in_attr, out_attr;
	int W, H, C;
	unsigned char *inbuf = NULL;
	rknn_input inputs[1];
	rknn_output outputs[1];
	FILE *fo;

	if (argc < 5) {
		fprintf(stderr, "usage: %s model.rknn img.rgb mask.gray out.rgb [normal] [N|loop]\n", argv[0]);
		return 1;
	}

	/* argv[6] 可选：
	 *   数字 N  → 同一输入连续跑 N 次（测量"纯推理"耗时，排查速度瓶颈）
	 *   "loop"  → 常驻模式：从 stdin 反复读 (img rgb + mask gray)，
	 *             处理后把结果写到 stdout，直到 EOF。
	 *             这样模型只加载一次，省掉每帧的 rknn_init。 */
	int repeat = 1;
	int loop_mode = 0;
	int perf_mode = 0;

	if (argc > 6) {
		if (!strcmp(argv[6], "loop"))
			loop_mode = 1;
		else if (!strcmp(argv[6], "perf"))
			perf_mode = 1;
		else
			repeat = atoi(argv[6]);
	}
	if (repeat < 1) repeat = 1;

	model = read_file(argv[1], &model_size);
	if (!model) return 1;

	ret = rknn_init(&ctx, model, model_size,
			perf_mode ? RKNN_FLAG_COLLECT_PERF_MASK : 0, NULL);
	if (ret < 0) {
		fprintf(stderr, "rknn_init failed: %d\n", ret);
		return 1;
	}

	memset(&io_num, 0, sizeof(io_num));
	ret = rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
	if (ret < 0) {
		fprintf(stderr, "query io_num failed: %d\n", ret);
		return 1;
	}

	memset(&in_attr, 0, sizeof(in_attr));
	in_attr.index = 0;
	ret = rknn_query(ctx, RKNN_QUERY_INPUT_ATTR, &in_attr, sizeof(in_attr));
	if (ret < 0) {
		fprintf(stderr, "query input attr failed: %d\n", ret);
		return 1;
	}

	if (in_attr.fmt == RKNN_TENSOR_NCHW) {
		C = in_attr.dims[1];
		H = in_attr.dims[2];
		W = in_attr.dims[3];
	} else {
		H = in_attr.dims[1];
		W = in_attr.dims[2];
		C = in_attr.dims[3];
	}
	fprintf(stderr, "input %dx%d ch=%d type=%d fmt=%d\n", W, H, C, in_attr.type, in_attr.fmt);

	if (C != 4) {
		fprintf(stderr, "expected 4 input channels, model has %d\n", C);
		return 1;
	}

	size_t fsz = 0;

	if (!loop_mode) {
		img = read_file(argv[2], &fsz);
		mask = read_file(argv[3], &fsz);
		if (!img || !mask) return 1;
	}

	/*
	 * 模型期望 uint8 输入（input attr type=UINT8），mean/std 在转换时已烘进去：
	 *   ch0    = 255*(1-mask)  -> (v-127.5)/255 = 0.5-mask
	 *   ch1..3 = 洞内 127（=> 0.0）、洞外原始像素 -> (v-127.5)/127.5
	 * argv[5] 可选 "inv"：把 ch0 极性反过来（调试用）。
	 */
	int inv = !(argc > 5 && strcmp(argv[5], "normal") == 0);

	inbuf = malloc((size_t)W * H * 4);
	if (!inbuf) return 1;

	/* ---------- 常驻模式：模型只加载一次，逐帧从 stdin 收、往 stdout 发 ---------- */
	if (loop_mode) {
		unsigned char *imgb = malloc((size_t)W * H * 3);
		unsigned char *mskb = malloc((size_t)W * H);
		unsigned char *obuf = malloc((size_t)W * H * 3);
		int oc, oh, ow;

		if (!imgb || !mskb || !obuf) return 1;
		memset(&out_attr, 0, sizeof(out_attr));
		out_attr.index = 0;
		rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &out_attr, sizeof(out_attr));
		if (out_attr.fmt == RKNN_TENSOR_NCHW) {
			oc = out_attr.dims[1]; oh = out_attr.dims[2]; ow = out_attr.dims[3];
		} else {
			oh = out_attr.dims[1]; ow = out_attr.dims[2]; oc = out_attr.dims[3];
		}
		fprintf(stderr, "loop mode: in %dx%d out %dx%d\n", W, H, ow, oh);

		memset(inputs, 0, sizeof(inputs));
		inputs[0].index = 0;
		inputs[0].type = RKNN_TENSOR_UINT8;
		inputs[0].size = (uint32_t)W * H * 4;
		inputs[0].fmt = RKNN_TENSOR_NHWC;
		inputs[0].buf = inbuf;
		inputs[0].pass_through = 0;

		while (fread(imgb, 1, (size_t)W * H * 3, stdin) == (size_t)W * H * 3 &&
		       fread(mskb, 1, (size_t)W * H, stdin) == (size_t)W * H) {
			for (int i = 0; i < W * H; i++) {
				int hole = mskb[i] < 128;
				unsigned char keep = hole ? 0 : 255;

				inbuf[i * 4 + 0] = inv ? keep : (unsigned char)(255 - keep);
				inbuf[i * 4 + 1] = imgb[i * 3 + 0];
				inbuf[i * 4 + 2] = imgb[i * 3 + 1];
				inbuf[i * 4 + 3] = imgb[i * 3 + 2];
			}
			if (rknn_inputs_set(ctx, 1, inputs) < 0) break;
			if (rknn_run(ctx, NULL) < 0) break;

			memset(outputs, 0, sizeof(outputs));
			outputs[0].index = 0;
			outputs[0].want_float = 1;
			if (rknn_outputs_get(ctx, 1, outputs, NULL) < 0) break;

			float *f = (float *)outputs[0].buf;
			for (int y = 0; y < oh; y++)
				for (int x = 0; x < ow; x++)
					for (int c = 0; c < 3; c++) {
						float v = (out_attr.fmt == RKNN_TENSOR_NCHW)
							? f[(c * oh + y) * ow + x]
							: f[(y * ow + x) * oc + c];
						int val = (int)(v * 127.5f + 127.5f + 0.5f);

						if (val < 0) val = 0;
						if (val > 255) val = 255;
						obuf[(y * ow + x) * 3 + c] = (unsigned char)val;
					}
			rknn_outputs_release(ctx, 1, outputs);
			fwrite(obuf, 1, (size_t)oh * ow * 3, stdout);
			fflush(stdout);
		}
		return 0;
	}

	for (int i = 0; i < W * H; i++) {
		int hole = mask[i] < 128;          /* mask 255 = keep, 0 = hole */
		unsigned char keep = hole ? 0 : 255;

		inbuf[i * 4 + 0] = inv ? keep : (unsigned char)(255 - keep);
		inbuf[i * 4 + 1] = img[i * 3 + 0];
		inbuf[i * 4 + 2] = img[i * 3 + 1];
		inbuf[i * 4 + 3] = img[i * 3 + 2];
	}

	memset(inputs, 0, sizeof(inputs));
	inputs[0].index = 0;
	inputs[0].type = RKNN_TENSOR_UINT8;
	inputs[0].size = (uint32_t)W * H * 4;
	inputs[0].fmt = RKNN_TENSOR_NHWC;
	inputs[0].buf = inbuf;
	inputs[0].pass_through = 0;

	ret = rknn_inputs_set(ctx, 1, inputs);
	if (ret < 0) {
		fprintf(stderr, "inputs_set failed: %d\n", ret);
		return 1;
	}

	ret = rknn_run(ctx, NULL);
	if (ret < 0) {
		fprintf(stderr, "rknn_run failed: %d\n", ret);
		return 1;
	}

	memset(outputs, 0, sizeof(outputs));
	outputs[0].index = 0;
	outputs[0].want_float = 1;
	ret = rknn_outputs_get(ctx, 1, outputs, NULL);
	if (ret < 0) {
		fprintf(stderr, "outputs_get failed: %d\n", ret);
		return 1;
	}

	memset(&out_attr, 0, sizeof(out_attr));
	out_attr.index = 0;
	rknn_query(ctx, RKNN_QUERY_OUTPUT_ATTR, &out_attr, sizeof(out_attr));

	float *f = (float *)outputs[0].buf;
	int oc, oh, ow;

	if (out_attr.fmt == RKNN_TENSOR_NCHW) {
		oc = out_attr.dims[1];
		oh = out_attr.dims[2];
		ow = out_attr.dims[3];
	} else {
		oh = out_attr.dims[1];
		ow = out_attr.dims[2];
		oc = out_attr.dims[3];
	}
	fprintf(stderr, "output %dx%d ch=%d\n", ow, oh, oc);

	fo = fopen(argv[4], "wb");
	if (!fo) {
		perror(argv[4]);
		return 1;
	}

	for (int y = 0; y < oh; y++) {
		for (int x = 0; x < ow; x++) {
			for (int c = 0; c < 3; c++) {
				float v;
				int val;

				if (out_attr.fmt == RKNN_TENSOR_NCHW)
					v = f[(c * oh + y) * ow + x];
				else
					v = f[(y * ow + x) * oc + c];

				val = (int)(v * 127.5f + 127.5f + 0.5f);
				if (val < 0) val = 0;
				if (val > 255) val = 255;
				fputc(val, fo);
			}
		}
	}
	fclose(fo);

	rknn_outputs_release(ctx, 1, outputs);

	if (perf_mode) {
		rknn_perf_detail pd;
		rknn_perf_run pr;

		memset(&pd, 0, sizeof(pd));
		if (rknn_query(ctx, RKNN_QUERY_PERF_DETAIL, &pd, sizeof(pd)) == 0 && pd.perf_data)
			fwrite(pd.perf_data, 1, (size_t)pd.data_len, stderr);
		memset(&pr, 0, sizeof(pr));
		if (rknn_query(ctx, RKNN_QUERY_PERF_RUN, &pr, sizeof(pr)) == 0)
			fprintf(stderr, "\nTOTAL_RUN_US %lld\n", (long long)pr.run_duration);
		return 0;
	}
	rknn_destroy(ctx);
	free(inbuf);
	free(img);
	free(mask);
	free(model);
	return 0;
}
