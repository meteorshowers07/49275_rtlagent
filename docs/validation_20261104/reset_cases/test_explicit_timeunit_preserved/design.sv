module counter(input logic clk, rst, en, output logic [3:0] q);
timeunit 1ns; timeprecision 1ps;
always_ff @(posedge clk)
  if (rst) q <= 4'd0;
  else if (en) q <= q + 4'd1;
endmodule
