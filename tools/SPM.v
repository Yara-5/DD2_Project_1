/*

    Copyright 2016, mshalan@aucegypt.edu

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
*/

/*  
    A serial-parallel multiplier modeled after: https://t.ly/inylp
*/

`timescale 1ns/1ps
`default_nettype none

module SPM(clk, rst, X, y, p);
    parameter SIZE = 32;
    input clk, rst;
    input y;
    input[SIZE-1 : 0] X;
    output p;

    wire[SIZE-1 : 1] pp;
    wire[SIZE-1 : 0] xy;

    genvar i;

    CSADD csa0 (.clk(clk), .rst(rst), .x(X[0]&y), .y(pp[1]), .sum(p));
    
    generate 
        for(i=1; i<SIZE-1; i=i+1) begin
            CSADD csa (.clk(clk), .rst(rst), .x(X[i]&y), .y(pp[i+1]), .sum(pp[i]));
        end 
    endgenerate
    
    TCMP tcmp (.clk(clk), .rst(rst), .a(X[SIZE-1]&y), .s(pp[SIZE-1]));

endmodule

