`timescale 1ns/1ps
// Handwritten reference for EDA smoke, never model performance evidence.
module counter(input logic clk, rst, en, output logic [3:0] q);
    always_ff @(posedge clk) begin
        if (rst) q <= 4'b0;
        else if (en) q <= q + 1'b1;
    end
endmodule
