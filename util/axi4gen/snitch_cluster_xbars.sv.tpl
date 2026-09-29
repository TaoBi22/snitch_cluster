`include "common_cells/assertions.svh"
% for x in selectors:

/// `${x['name']}` of the cluster at `BaseAddr`, ported as `axi_xbar`.
module ${x['name']} #(
  parameter logic [${addr_width - 1}:0] BaseAddr = '0,
% for name, (type, default, _) in x['checks'].items():
  parameter ${type} ${name} = ${default},
% endfor
  parameter type slv_req_t  = logic,
  parameter type slv_resp_t = logic,
  parameter type mst_req_t  = logic,
  parameter type mst_resp_t = logic
) (
  input  logic                           clk_i,
  input  logic                           rst_ni,
  input  slv_req_t  [${len(x['mgrs']) - 1}:0] slv_ports_req_i,
  output slv_resp_t [${len(x['mgrs']) - 1}:0] slv_ports_resp_o,
  output mst_req_t  [${len(x['subs']) - 1}:0] mst_ports_req_o,
  input  mst_resp_t [${len(x['subs']) - 1}:0] mst_ports_resp_i
);
  // The generated ports are anonymous structs, which pad or truncate silently.
  `ASSERT_INIT(SlvBits, $bits(slv_req_t) == ${x['mgr_bits'][0]} && $bits(slv_resp_t) == ${x['mgr_bits'][1]})
  `ASSERT_INIT(MstBits, $bits(mst_req_t) == ${x['sub_bits'][0]} && $bits(mst_resp_t) == ${x['sub_bits'][1]})
% for name, (_, _, value) in x['checks'].items():
  `ASSERT_INIT(Generated${name}, ${name} == ${value})
% endfor

  case (BaseAddr)
% for k, base in enumerate(bases):
    ${addr_width}'h${'{:x}'.format(base)}: begin : gen_${k}
      ${x['name']}_${k} i_xbar (
        .clk    (clk_i),
        .rst_ni (rst_ni),
<% conns = [f"        .{p}_req (slv_ports_req_i[{e}]),\n        .{p}_resp (slv_ports_resp_o[{e}])" for p, e in x['mgrs'].items()] + \
           [f"        .{p}_req (mst_ports_req_o[{e}]),\n        .{p}_resp (mst_ports_resp_i[{e}])" for p, e in x['subs'].items()] %>\
${',\n'.join(conns)}
      );
    end
% endfor
    default: begin : gen_unsupported
      initial $fatal(1, "No generated ${x['name']} for BaseAddr %0h", BaseAddr);
    end
  endcase
endmodule
% endfor
